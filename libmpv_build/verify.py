"""Pure-Python ELF64/PE32+ binary verification gates and Linux ctypes smoke test."""

from __future__ import annotations

import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import ROOT
from .config import (
    ANDROID_SYSTEM_LIBRARIES,
    LINUX_SYSTEM_LIBRARIES,
    MAX_GLIBC_VERSION,
    REQUIRED_MPV_EXPORTS,
    TARGETS,
    WINDOWS_SYSTEM_DLL_PREFIXES,
    WINDOWS_SYSTEM_DLLS,
    load_profiles,
)


class VerificationError(RuntimeError):
    """Raised when a built runtime or archive fails binary/ABI/header gates."""


@dataclass(frozen=True)
class BinaryInfo:
    kind: str  # "elf64" | "pe64"
    machine: int
    is_dll: bool
    pt_load_aligns: tuple[int, ...]
    needed_libs: tuple[str, ...]
    exports: frozenset[str]
    glibc_versions: tuple[str, ...]
    elf_dynamic_tags: frozenset[int]


def _require_range(blob: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(blob) or size > len(blob) - offset:
        raise VerificationError(f"Truncated or invalid {label}")


def _read_cstr(blob: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(blob):
        raise VerificationError("Invalid binary string offset")
    end = blob.find(b"\x00", offset)
    if end == -1:
        raise VerificationError("Unterminated binary string")
    try:
        return blob[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("Invalid binary string encoding") from exc


def inspect_elf64(path: Path) -> BinaryInfo:
    data = path.read_bytes()
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise VerificationError(f"Not an ELF binary: {path}")
    if data[4] != 2 or data[5] != 1 or data[6] != 1:
        raise VerificationError(f"Expected 64-bit little-endian ELF: {path}")

    (
        e_type,
        e_machine,
        version,
        _,
        e_phoff,
        e_shoff,
        _,
        e_ehsize,
        e_phentsize,
        e_phnum,
        e_shentsize,
        e_shnum,
        _,
    ) = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    if (
        version != 1
        or e_ehsize != 64
        or e_phentsize != 56
        or e_shentsize != 64
        or not e_phnum
        or not e_shnum
    ):
        raise VerificationError(f"Unsupported or invalid ELF tables: {path}")
    _require_range(data, e_phoff, e_phnum * e_phentsize, "ELF program headers")
    _require_range(data, e_shoff, e_shnum * e_shentsize, "ELF section headers")

    pt_load_aligns: list[int] = []
    load_segments: list[tuple[int, int, int, int]] = []
    dynamic_segments: list[tuple[int, int]] = []
    has_interpreter = False
    for index in range(e_phnum):
        off = e_phoff + index * e_phentsize
        p_type, p_flags, p_offset, p_vaddr, _, p_filesz, p_memsz, p_align = (
            struct.unpack_from("<IIQQQQQQ", data, off)
        )
        _require_range(data, p_offset, p_filesz, "ELF program segment")
        if p_type == 1:  # PT_LOAD
            if p_filesz > p_memsz or (
                p_align > 1
                and (p_align & (p_align - 1) or p_offset % p_align != p_vaddr % p_align)
            ):
                raise VerificationError("Invalid ELF PT_LOAD mapping or alignment")
            pt_load_aligns.append(p_align)
            load_segments.append((p_vaddr, p_offset, p_filesz, p_flags))
        elif p_type == 2:  # PT_DYNAMIC
            dynamic_segments.append((p_offset, p_filesz))
        elif (
            p_type == 3
        ):  # PT_INTERP marks an executable rather than a loadable runtime
            has_interpreter = True

    sections: list[tuple[int, int, int, int, int, int, int]] = []
    for index in range(e_shnum):
        off = e_shoff + index * e_shentsize
        _, sh_type, _, sh_addr, sh_offset, sh_size, sh_link, sh_info, _, sh_entsize = (
            struct.unpack_from("<IIQQQQIIQQ", data, off)
        )
        if sh_type not in (0, 8):  # SHT_NULL / SHT_NOBITS have no file payload
            _require_range(data, sh_offset, sh_size, "ELF section")
        sections.append(
            (sh_type, sh_offset, sh_size, sh_link, sh_info, sh_entsize, sh_addr)
        )

    dynamic_sections = [section for section in sections if section[0] == 6]
    symbol_sections = [section for section in sections if section[0] == 11]
    if (
        len(dynamic_segments) != 1
        or len(dynamic_sections) != 1
        or len(symbol_sections) != 1
        or dynamic_segments[0] != dynamic_sections[0][1:3]
    ):
        raise VerificationError(
            "ELF dynamic tables must match the runtime PT_DYNAMIC segment"
        )

    def require_runtime_mapping(
        section: tuple[int, int, int, int, int, int, int],
    ) -> None:
        _, file_offset, size, _, _, _, address = section
        if not any(
            address >= virtual_address
            and size <= file_size - (address - virtual_address)
            and file_offset == segment_offset + address - virtual_address
            for virtual_address, segment_offset, file_size, _ in load_segments
        ):
            raise VerificationError(
                "ELF dynamic section differs from the runtime PT_LOAD mapping"
            )

    for section in (*dynamic_sections, *symbol_sections):
        require_runtime_mapping(section)

    needed: list[str] = []
    exports: set[str] = set()
    glibc_versions: set[str] = set()
    dynamic_values: dict[int, int] = {}
    dynamic_tags: set[int] = set()
    symbol_names: list[str] = []
    visible_symbol_indices: set[int] = set()

    for sh_type, sh_offset, sh_size, sh_link, sh_info, sh_entsize, _ in sections:
        if sh_type in (6, 11, 0x6FFFFFFE) and (
            sh_link >= len(sections) or sections[sh_link][0] != 3
        ):
            raise VerificationError("Invalid ELF linked string table")
        if sh_type == 6:  # SHT_DYNAMIC
            _, str_off, str_size, _, _, _, _ = sections[sh_link]
            dynstr = data[str_off : str_off + str_size]
            step = sh_entsize or 16
            if step != 16 or sh_size % step:
                raise VerificationError("Invalid ELF dynamic table size")
            terminated = False
            for pos in range(sh_offset, sh_offset + sh_size, step):
                d_tag, d_val = struct.unpack_from("<qQ", data, pos)
                if d_tag == 0:
                    terminated = True
                    break
                dynamic_tags.add(d_tag)
                if d_tag in (
                    0x7FFFFFFD,
                    0x7FFFFFFF,
                    0x6FFFFEFB,
                    0x6FFFFEFC,
                ):  # Filters and audit modules load outside DT_NEEDED
                    raise VerificationError(
                        "ELF dynamic filter or audit dependencies are not allowed in the standalone runtime"
                    )
                if d_tag in (4, 5, 6, 10, 11, 0x6FFFFEF5, 0x6FFFFFFE, 0x6FFFFFFF):
                    if d_tag in dynamic_values:
                        raise VerificationError("Duplicate ELF dynamic table tag")
                    dynamic_values[d_tag] = d_val
                if d_tag == 1:  # DT_NEEDED
                    name = _read_cstr(dynstr, d_val)
                    if name:
                        needed.append(name)
                    else:
                        raise VerificationError("Empty ELF DT_NEEDED library name")
            if not terminated:
                raise VerificationError("Unterminated ELF dynamic table")
        elif sh_type == 11:  # SHT_DYNSYM
            _, str_off, str_size, _, _, _, _ = sections[sh_link]
            dynstr = data[str_off : str_off + str_size]
            step = sh_entsize or 24
            if step != 24 or sh_size % step:
                raise VerificationError("Invalid ELF dynamic symbol table size")
            for pos in range(sh_offset, sh_offset + sh_size, step):
                st_name, st_info, st_other, st_shndx, st_value, st_size = (
                    struct.unpack_from("<IBBHQQ", data, pos)
                )
                bind = st_info >> 4
                visibility = st_other & 0x3
                sym = _read_cstr(dynstr, st_name)
                symbol_names.append(sym)
                if (
                    st_shndx != 0
                    and bind in (1, 2, 10)
                    and visibility in (0, 3)
                    and sym
                ):
                    if (
                        sym in REQUIRED_MPV_EXPORTS or sym == "av_jni_set_java_vm"
                    ) and (
                        (st_info & 0xF) not in (2, 10)
                        or not 0 < st_shndx < len(sections)
                        or not st_value
                        or not any(
                            flags & 1
                            and virtual_address
                            <= st_value
                            < virtual_address + file_size
                            and max(st_size, 1)
                            <= file_size - (st_value - virtual_address)
                            for virtual_address, _, file_size, flags in load_segments
                        )
                    ):
                        raise VerificationError(
                            f"ELF required export is not a mapped function: {sym}"
                        )
                    exports.add(sym)
                    visible_symbol_indices.add(len(symbol_names) - 1)
        elif sh_type == 0x6FFFFFFE:  # SHT_GNU_VERNEED
            _, str_off, str_size, _, _, _, _ = sections[sh_link]
            dynstr = data[str_off : str_off + str_size]
            pos = sh_offset
            section_end = sh_offset + sh_size
            if sh_info > sh_size // 16:
                raise VerificationError("Invalid ELF version requirement count")
            for requirement_index in range(sh_info):
                if pos < sh_offset or pos + 16 > section_end:
                    raise VerificationError("Truncated ELF version requirement")
                vn_version, vn_cnt, _, vn_aux, vn_next = struct.unpack_from(
                    "<HHIII", data, pos
                )
                aux_pos = pos + vn_aux
                if (
                    vn_version != 1
                    or not vn_cnt
                    or vn_cnt > sh_size // 16
                    or vn_aux < 16
                ):
                    raise VerificationError(
                        "Invalid ELF auxiliary version count or offset"
                    )
                for auxiliary_index in range(vn_cnt):
                    if aux_pos < pos + 16 or aux_pos + 16 > section_end:
                        raise VerificationError(
                            "Truncated ELF auxiliary version requirement"
                        )
                    _, _, _, vna_name, vna_next = struct.unpack_from(
                        "<IHHII", data, aux_pos
                    )
                    ver_name = _read_cstr(dynstr, vna_name)
                    if ver_name.startswith("GLIBC_"):
                        glibc_versions.add(ver_name)
                    if vna_next == 0:
                        if auxiliary_index + 1 != vn_cnt:
                            raise VerificationError(
                                "Truncated ELF auxiliary version chain"
                            )
                        break
                    if auxiliary_index + 1 == vn_cnt or vna_next < 16:
                        raise VerificationError("Invalid ELF auxiliary version chain")
                    aux_pos += vna_next
                if vn_next == 0:
                    if requirement_index + 1 != sh_info:
                        raise VerificationError(
                            "Truncated ELF version requirement chain"
                        )
                    break
                if requirement_index + 1 == sh_info or vn_next < 16:
                    raise VerificationError("Invalid ELF version requirement chain")
                pos += vn_next

    dynamic_section, symbol_section = dynamic_sections[0], symbol_sections[0]
    string_section = sections[dynamic_section[3]]
    require_runtime_mapping(string_section)
    if (
        dynamic_values.get(5) != string_section[6]
        or dynamic_values.get(10) != string_section[2]
        or dynamic_values.get(6) != symbol_section[6]
        or dynamic_values.get(11) != 24
        or symbol_section[3] != dynamic_section[3]
    ):
        raise VerificationError(
            "ELF section tables do not match runtime dynamic symbols or strings"
        )
    version_sections = [section for section in sections if section[0] == 0x6FFFFFFE]
    if 0x6FFFFFFE in dynamic_values or 0x6FFFFFFF in dynamic_values:
        if (
            len(version_sections) != 1
            or dynamic_values.get(0x6FFFFFFE) != version_sections[0][6]
            or dynamic_values.get(0x6FFFFFFF) != version_sections[0][4]
            or version_sections[0][3] != dynamic_section[3]
        ):
            raise VerificationError(
                "ELF version sections do not match runtime version requirements"
            )
        require_runtime_mapping(version_sections[0])
    elif any(section[2] for section in version_sections):
        raise VerificationError(
            "ELF version section has no runtime version requirements"
        )

    def runtime_offset(address: int, size: int) -> int:
        for virtual_address, segment_offset, file_size, _ in load_segments:
            delta = address - virtual_address
            if 0 <= delta and size <= file_size - delta:
                return segment_offset + delta
        raise VerificationError(
            "ELF runtime symbol hash table is outside the PT_LOAD mapping"
        )

    def symbol_is_reachable(name: str) -> bool:
        encoded = name.encode("utf-8")
        if (
            0x6FFFFEF5 in dynamic_values
        ):  # The loader prefers DT_GNU_HASH when both formats exist
            table = dynamic_values[0x6FFFFEF5]
            buckets, symbol_start, bloom_size, bloom_shift = struct.unpack_from(
                "<IIII", data, runtime_offset(table, 16)
            )
            if (
                not buckets
                or not bloom_size
                or bloom_size & (bloom_size - 1)
                or bloom_shift >= 32
                or symbol_start > len(symbol_names)
            ):
                raise VerificationError("Invalid ELF GNU symbol hash header")
            runtime_offset(table + 16, bloom_size * 8 + buckets * 4)
            hashed = 5381
            for byte in encoded:
                hashed = (hashed * 33 + byte) & 0xFFFFFFFF
            bloom = struct.unpack_from(
                "<Q",
                data,
                runtime_offset(table + 16 + ((hashed // 64) & (bloom_size - 1)) * 8, 8),
            )[0]
            mask = (1 << (hashed % 64)) | (1 << ((hashed >> bloom_shift) % 64))
            if (bloom & mask) != mask:
                return False
            bucket_base = table + 16 + bloom_size * 8
            index = struct.unpack_from(
                "<I", data, runtime_offset(bucket_base + (hashed % buckets) * 4, 4)
            )[0]
            if index == 0:
                return False
            if index < symbol_start:
                raise VerificationError("Invalid ELF GNU symbol hash bucket")
            chain_base = bucket_base + buckets * 4
            while index < len(symbol_names):
                chain = struct.unpack_from(
                    "<I",
                    data,
                    runtime_offset(chain_base + (index - symbol_start) * 4, 4),
                )[0]
                if (
                    (chain | 1) == (hashed | 1)
                    and symbol_names[index] == name
                    and index in visible_symbol_indices
                ):
                    return True
                if chain & 1:
                    return False
                index += 1
            raise VerificationError(
                "Unterminated or out-of-range ELF GNU symbol hash chain"
            )
        if 4 not in dynamic_values:
            raise VerificationError(
                "ELF runtime is missing its dynamic symbol hash table"
            )
        table = dynamic_values[4]
        buckets, chains = struct.unpack_from("<II", data, runtime_offset(table, 8))
        if not buckets or chains != len(symbol_names):
            raise VerificationError("Invalid ELF SysV symbol hash header")
        runtime_offset(table + 8, (buckets + chains) * 4)
        hashed = 0
        for byte in encoded:
            hashed = (hashed << 4) + byte
            high = hashed & 0xF0000000
            hashed = (hashed ^ (high >> 24)) & ~high
        index = struct.unpack_from(
            "<I", data, runtime_offset(table + 8 + (hashed % buckets) * 4, 4)
        )[0]
        visited: set[int] = set()
        while index:
            if index >= chains or index in visited:
                raise VerificationError("Invalid or cyclic ELF SysV symbol hash chain")
            visited.add(index)
            if symbol_names[index] == name and index in visible_symbol_indices:
                return True
            index = struct.unpack_from(
                "<I", data, runtime_offset(table + 8 + (buckets + index) * 4, 4)
            )[0]
        return False

    for name in sorted(exports & (REQUIRED_MPV_EXPORTS | {"av_jni_set_java_vm"})):
        if not symbol_is_reachable(name):
            raise VerificationError(
                f"ELF required export is unreachable through the runtime symbol hash table: {name}"
            )

    return BinaryInfo(
        kind="elf64",
        machine=e_machine,
        is_dll=e_type == 3 and not has_interpreter,
        pt_load_aligns=tuple(pt_load_aligns),
        needed_libs=tuple(needed),
        exports=frozenset(exports),
        glibc_versions=tuple(sorted(glibc_versions, key=_parse_glibc_version)),
        elf_dynamic_tags=frozenset(dynamic_tags),
    )


def inspect_pe64(path: Path) -> BinaryInfo:
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        raise VerificationError(f"Not a PE binary: {path}")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        raise VerificationError(f"Invalid PE signature: {path}")

    machine, num_sections, _, _, _, size_opt_hdr, characteristics = struct.unpack_from(
        "<HHIIIHH", data, e_lfanew + 4
    )
    opt_off = e_lfanew + 24
    if opt_off + size_opt_hdr > len(data) or size_opt_hdr < 112:
        raise VerificationError(f"Invalid PE optional header: {path}")
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic != 0x20B:
        raise VerificationError(f"Expected PE32+ (64-bit) binary: {path}")

    num_rva_and_sizes = struct.unpack_from("<I", data, opt_off + 108)[0]
    if num_rva_and_sizes > 16 or 112 + num_rva_and_sizes * 8 > size_opt_hdr:
        raise VerificationError("Invalid PE data directory table size")
    image_base = struct.unpack_from("<Q", data, opt_off + 24)[0]
    data_dirs: list[tuple[int, int]] = []
    for idx in range(num_rva_and_sizes):
        rva, size = struct.unpack_from("<II", data, opt_off + 112 + idx * 8)
        data_dirs.append((rva, size))

    sec_off = opt_off + size_opt_hdr
    _require_range(data, sec_off, num_sections * 40, "PE section headers")
    sections: list[tuple[int, int, int, int, int]] = []
    for idx in range(num_sections):
        off = sec_off + idx * 40
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        section_flags = struct.unpack_from("<I", data, off + 36)[0]
        _require_range(data, raw_ptr, raw_size, "PE section payload")
        sections.append((va, vsize, raw_ptr, raw_size, section_flags))

    def rva_to_offset(rva: int, size: int = 1) -> int:
        if rva <= 0:
            raise VerificationError("Invalid null PE RVA")
        for va, vsize, raw_ptr, raw_size, _ in sections:
            span = max(vsize, raw_size)
            if va <= rva < va + span:
                delta = rva - va
                if size <= raw_size - delta:
                    return raw_ptr + delta
                raise VerificationError("PE RVA extends beyond section data")
        if rva + size <= sec_off + num_sections * 40:
            return rva
        raise VerificationError(f"Unmapped PE RVA: 0x{rva:x}")

    def rva_string(rva: int) -> str:
        offset = rva_to_offset(rva)
        for va, _, raw_ptr, raw_size, _ in sections:
            if raw_ptr <= offset < raw_ptr + raw_size:
                return _read_cstr(data[raw_ptr : raw_ptr + raw_size], offset - raw_ptr)
        return _read_cstr(data[: sec_off + num_sections * 40], offset)

    def directory(index: int) -> tuple[int, int] | None:
        if index >= len(data_dirs) or data_dirs[index] == (0, 0):
            return None
        rva, size = data_dirs[index]
        if not rva or not size:
            raise VerificationError("Incomplete PE data directory")
        return rva_to_offset(rva, size), size

    exports: set[str] = set()
    export_directory = directory(0)
    if export_directory is not None:
        exp_off, exp_size = export_directory
        if exp_size < 40:
            raise VerificationError("Truncated PE export directory")
        fields = struct.unpack_from("<IIHHIIIIIII", data, exp_off)
        num_functions, num_names = fields[6:8]
        functions_off = rva_to_offset(fields[8], num_functions * 4)
        names_off = rva_to_offset(fields[9], num_names * 4)
        ordinals_off = rva_to_offset(fields[10], num_names * 2)
        named_ordinals: set[int] = set()
        for idx in range(num_names):
            ordinal = struct.unpack_from("<H", data, ordinals_off + idx * 2)[0]
            if ordinal >= num_functions:
                raise VerificationError("Invalid PE export ordinal")
            named_ordinals.add(ordinal)
            function_rva = struct.unpack_from("<I", data, functions_off + ordinal * 4)[
                0
            ]
            rva_to_offset(function_rva)
            if data_dirs[0][0] <= function_rva < data_dirs[0][0] + exp_size:
                raise VerificationError(
                    "Forwarded PE exports are not allowed in the standalone runtime"
                )
            name_rva = struct.unpack_from("<I", data, names_off + idx * 4)[0]
            symbol = rva_string(name_rva)
            if not symbol or symbol in exports:
                raise VerificationError("Empty or duplicate PE export name")
            if symbol in REQUIRED_MPV_EXPORTS and not any(
                flags & 0x20000000 and va <= function_rva < va + raw_size
                for va, _, _, raw_size, flags in sections
            ):
                raise VerificationError(
                    f"PE required export is not a mapped function in an executable section: {symbol}"
                )
            exports.add(symbol)
        for ordinal in range(num_functions):
            function_rva = struct.unpack_from("<I", data, functions_off + ordinal * 4)[
                0
            ]
            if function_rva and ordinal not in named_ordinals:
                raise VerificationError(
                    "Unnamed PE ordinal exports are not allowed in the standalone runtime"
                )

    needed: list[str] = []
    for index, entry_size in ((1, 20), (13, 32)):
        import_directory = directory(index)
        if import_directory is None:
            continue
        imp_off, imp_size = import_directory
        terminated = False
        for pos in range(imp_off, imp_off + imp_size - entry_size + 1, entry_size):
            fields = struct.unpack_from("<" + "I" * (entry_size // 4), data, pos)
            if not any(fields):
                terminated = True
                break
            if index == 1:
                name_rva = fields[3]
            else:
                attributes, name_rva = fields[:2]
                if attributes & ~1:
                    raise VerificationError("Invalid PE delay import attributes")
                if not attributes & 1:
                    name_rva -= image_base
            dll = rva_string(name_rva)
            if not dll:
                raise VerificationError("Empty PE import DLL name")
            needed.append(dll)
        if not terminated:
            raise VerificationError("Unterminated PE import directory")

    return BinaryInfo(
        kind="pe64",
        machine=machine,
        is_dll=bool(characteristics & 0x2000),
        pt_load_aligns=(),
        needed_libs=tuple(needed),
        exports=frozenset(exports),
        glibc_versions=(),
        elf_dynamic_tags=frozenset(),
    )


def verify_headers_parity(built_headers_dir: Path, repo_headers_dir: Path) -> list[str]:
    errors: list[str] = []
    if not built_headers_dir.is_dir():
        return [f"Missing built headers directory: {built_headers_dir}"]
    if not repo_headers_dir.is_dir():
        return [f"Missing repository headers directory: {repo_headers_dir}"]
    built = {
        p.relative_to(built_headers_dir).as_posix(): p.read_bytes()
        for p in sorted(built_headers_dir.rglob("*.h"))
    }
    repo = {
        p.relative_to(repo_headers_dir).as_posix(): p.read_bytes()
        for p in sorted(repo_headers_dir.rglob("*.h"))
    }
    required_headers = {"client.h", "render.h", "stream_cb.h"}
    if not required_headers <= set(built) or not required_headers <= set(repo):
        errors.append("Public headers must include client.h, render.h and stream_cb.h")
    if set(built) != set(repo):
        missing = sorted(set(built) - set(repo))
        extra = sorted(set(repo) - set(built))
        errors.append(f"Public header set mismatch (missing={missing}, extra={extra})")
    for rel in sorted(set(built) & set(repo)):
        if built[rel] != repo[rel]:
            errors.append(
                f"Public header content differs from built mpv: include/mpv/{rel}"
            )
    return errors


def run_linux_smoke(library_path: Path) -> None:
    script = (
        "import ctypes, sys\n"
        "def require(condition, message):\n"
        "    if not condition:\n"
        "        raise RuntimeError(message)\n"
        f"lib = ctypes.CDLL({str(library_path.resolve())!r})\n"
        "lib.mpv_client_api_version.restype = ctypes.c_ulong\n"
        "ver = lib.mpv_client_api_version()\n"
        "require(ver > 0, f'bad api version: {ver}')\n"
        "lib.mpv_create.restype = ctypes.c_void_p\n"
        "ctx = lib.mpv_create()\n"
        "require(ctx, 'mpv_create returned NULL')\n"
        "lib.mpv_set_option_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]\n"
        "lib.mpv_set_option_string.restype = ctypes.c_int\n"
        "for name, value in ((b'config', b'no'), (b'load-scripts', b'no'), (b'vo', b'null'), (b'ao', b'null')):\n"
        "    rc = lib.mpv_set_option_string(ctx, name, value)\n"
        "    require(rc == 0 or (name == b'load-scripts' and rc == -5), f'bad option: {name!r}: {rc}')\n"
        "lib.mpv_initialize.argtypes = [ctypes.c_void_p]\n"
        "lib.mpv_initialize.restype = ctypes.c_int\n"
        "rc = lib.mpv_initialize(ctx)\n"
        "require(rc == 0, f'mpv_initialize failed: {rc}')\n"
        "lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]\n"
        "lib.mpv_terminate_destroy(ctx)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(
            "Linux runtime smoke test timed out after 30 seconds"
        ) from exc
    if proc.returncode != 0:
        raise VerificationError(
            f"Linux runtime smoke test failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )


def _parse_glibc_version(version_str: str) -> tuple[int, ...]:
    match = re.fullmatch(r"GLIBC_(\d+(?:\.\d+)*)", version_str)
    if not match:
        return (999, 999)
    return tuple(int(part) for part in match.group(1).split("."))


def _is_allowed_windows_dll(name: str) -> bool:
    lower = name.lower()
    return lower in WINDOWS_SYSTEM_DLLS or (
        re.fullmatch(r"[a-z0-9-]+\.dll", lower) is not None
        and any(lower.startswith(prefix) for prefix in WINDOWS_SYSTEM_DLL_PREFIXES)
    )


def verify_library_file(
    target: str,
    library_path: Path,
    *,
    built_headers_dir: Path | None = None,
    repo_headers_dir: Path | None = None,
    run_smoke: bool = False,
) -> dict:
    if target not in TARGETS:
        raise VerificationError(f"Unknown target: {target}")
    if (built_headers_dir is None) != (repo_headers_dir is None):
        raise VerificationError(
            "Both built and repository header directories are required for header parity"
        )
    profile = load_profiles(ROOT / "targets.json")[target]
    errors: list[str] = []

    if profile.system == "windows":
        info = inspect_pe64(library_path)
        if info.machine != 0x8664 or not info.is_dll:
            errors.append(
                f"Expected PE32+ AMD64 DLL, got machine=0x{info.machine:x}, is_dll={info.is_dll}"
            )
        disallowed = sorted(
            {dll for dll in info.needed_libs if not _is_allowed_windows_dll(dll)}
        )
        if disallowed:
            errors.append(f"Disallowed Windows DLL imports: {disallowed}")
    else:
        info = inspect_elf64(library_path)
        if not info.is_dll or not info.pt_load_aligns:
            errors.append("Expected an ELF shared library with loadable segments")
        expected_machine = 183 if profile.cpu == "aarch64" else 62
        if info.machine != expected_machine:
            errors.append(
                f"ELF machine mismatch: expected {expected_machine}, got {info.machine}"
            )
        if info.elf_dynamic_tags & {35, 36, 37}:
            if (
                profile.system == "android"
                and profile.api is not None
                and profile.api < 30
            ):
                errors.append(
                    "Standard DT_RELR requires Android API 30 or newer; API 28 must use Android RELR tags"
                )
            elif profile.system == "linux" and MAX_GLIBC_VERSION < (2, 36):
                errors.append("Standard DT_RELR requires glibc 2.36 or newer")
        if profile.system == "android":
            bad_aligns = [
                align
                for align in info.pt_load_aligns
                if align < 16384 or align % 16384 != 0
            ]
            if not info.pt_load_aligns or bad_aligns:
                errors.append(
                    f"Android 16 KiB PT_LOAD alignment check failed: {info.pt_load_aligns}"
                )
            disallowed = sorted(set(info.needed_libs) - ANDROID_SYSTEM_LIBRARIES)
            if disallowed:
                errors.append(f"Disallowed Android DT_NEEDED libraries: {disallowed}")
            if "av_jni_set_java_vm" not in info.exports:
                errors.append("Android build must export av_jni_set_java_vm")
        else:
            disallowed = sorted(set(info.needed_libs) - LINUX_SYSTEM_LIBRARIES)
            if disallowed:
                errors.append(f"Disallowed Linux DT_NEEDED libraries: {disallowed}")
            bad_glibc = [
                ver
                for ver in info.glibc_versions
                if _parse_glibc_version(ver) > MAX_GLIBC_VERSION
            ]
            if bad_glibc:
                errors.append(
                    f"Linux GLIBC symbol version exceeds {MAX_GLIBC_VERSION}: {bad_glibc}"
                )

    missing_mpv = sorted(REQUIRED_MPV_EXPORTS - info.exports)
    if missing_mpv:
        errors.append(f"Missing required mpv C API exports: {missing_mpv}")

    allowed_extra = {"av_jni_set_java_vm"} if profile.system == "android" else set()
    leaked = sorted(
        sym
        for sym in info.exports
        if not sym.startswith("mpv_") and sym not in allowed_extra
    )
    if leaked:
        errors.append(f"Leaked non-mpv exported symbols: {leaked[:20]}")

    if built_headers_dir is not None and repo_headers_dir is not None:
        errors.extend(verify_headers_parity(built_headers_dir, repo_headers_dir))

    if errors:
        raise VerificationError(
            f"[{target}] Binary verification failed:\n- " + "\n- ".join(errors)
        )

    smoke_ran = False
    if run_smoke and target == "linux-x64":
        run_linux_smoke(library_path)
        smoke_ran = True

    return {
        "target": target,
        "library": profile.library,
        "bytes": library_path.stat().st_size,
        "neededCount": len(info.needed_libs),
        "exportCount": len(info.exports),
        "glibcMax": info.glibc_versions[-1] if info.glibc_versions else None,
        "smokePassed": smoke_ran,
    }


def verify_path(
    target: str,
    path: Path,
    *,
    built_headers_dir: Path | None = None,
    repo_headers_dir: Path | None = None,
    run_smoke: bool = False,
) -> dict:
    """Verify either a raw shared library or a flat delivery ZIP archive."""
    if target not in TARGETS:
        raise VerificationError(f"Unknown target: {target}")
    profile = load_profiles(ROOT / "targets.json")[target]
    if path.suffix != ".zip":
        return verify_library_file(
            target,
            path,
            built_headers_dir=built_headers_dir,
            repo_headers_dir=repo_headers_dir,
            run_smoke=run_smoke,
        )
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if (
            len(entries) != 1
            or entries[0].filename != profile.library
            or entries[0].is_dir()
            or stat.S_ISLNK(entries[0].external_attr >> 16)
        ):
            raise VerificationError(
                f"[{target}] Delivery ZIP must contain only flat {profile.library}, got {[e.filename for e in entries]}"
            )
        with tempfile.TemporaryDirectory(prefix=".verify-") as tmpdir:
            extracted = Path(tmpdir) / profile.library
            with archive.open(entries[0]) as incoming, extracted.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            return verify_library_file(
                target,
                extracted,
                built_headers_dir=built_headers_dir,
                repo_headers_dir=repo_headers_dir,
                run_smoke=run_smoke,
            )
