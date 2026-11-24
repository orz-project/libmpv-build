from __future__ import annotations

import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from libmpv_build.config import REQUIRED_MPV_EXPORTS
from libmpv_build.verify import (
    VerificationError,
    run_linux_smoke,
    verify_headers_parity,
    verify_library_file,
    verify_path,
)


def _build_synthetic_elf64(
    path: Path,
    *,
    machine: int,
    pt_load_align: int = 0x4000,
    needed: tuple[str, ...] = (),
    exports: tuple[str, ...] = (),
    glibc_versions: tuple[str, ...] = (),
    hash_style: str = "gnu",
) -> None:
    dynstr_parts = [b"\x00"]
    offsets: dict[str, int] = {}

    def intern(s: str) -> int:
        if s not in offsets:
            offsets[s] = sum(len(p) for p in dynstr_parts)
            dynstr_parts.append(s.encode("utf-8") + b"\x00")
        return offsets[s]

    for item in (*needed, *exports, *glibc_versions):
        intern(item)
    dynstr = b"".join(dynstr_parts)

    dynamic_size = (len(needed) + 6 + (2 if glibc_versions else 0)) * 16
    dynsym_entries = struct.pack("<IBBHQQ", 0, 0, 0, 0, 0, 0) + b"".join(
        struct.pack("<IBBHQQ", intern(sym), (1 << 4) | 2, 0, 1, 0, 0) for sym in exports
    )

    verneed_bytes = b""
    if glibc_versions:
        verneed_bytes = bytearray()
        verneed_bytes += struct.pack("<HHIII", 1, len(glibc_versions), 0, 16, 0)
        for idx, ver in enumerate(glibc_versions):
            nxt = 16 if idx + 1 < len(glibc_versions) else 0
            verneed_bytes += struct.pack("<IHHII", 0, 0, idx + 2, intern(ver), nxt)
        verneed_bytes = bytes(verneed_bytes)

    ehdr_size = 64
    phdr_size = 56
    shdr_size = 64
    phoff = ehdr_size
    data_off = phoff + phdr_size * 2

    dynstr_off = data_off
    dyn_off = dynstr_off + len(dynstr)
    dynsym_off = dyn_off + dynamic_size
    verneed_off = dynsym_off + len(dynsym_entries)
    code_off = verneed_off + len(verneed_bytes)
    code = b"\xc0\x03\x5f\xd6" if machine == 183 else b"\xc3"
    hash_off = code_off + len(code)
    if hash_style == "gnu":
        hashes = []
        bloom = 0
        for symbol in exports:
            hashed = 5381
            for byte in symbol.encode("utf-8"):
                hashed = (hashed * 33 + byte) & 0xFFFFFFFF
            hashes.append(hashed)
            bloom |= (1 << (hashed % 64)) | (1 << ((hashed >> 5) % 64))
        chain = b"".join(
            struct.pack("<I", (hashed & ~1) | (index == len(hashes) - 1))
            for index, hashed in enumerate(hashes)
        )
        symbol_hash = (
            struct.pack("<IIIIQI", 1, 1, 1, 5, bloom, 1 if exports else 0) + chain
        )
        hash_tag, hash_type = 0x6FFFFEF5, 0x6FFFFFF6
    else:
        chains = [
            0,
            *(
                index + 1 if index < len(exports) else 0
                for index in range(1, len(exports) + 1)
            ),
        ]
        symbol_hash = struct.pack("<III", 1, len(exports) + 1, 1 if exports else 0)
        symbol_hash += b"".join(struct.pack("<I", index) for index in chains)
        hash_tag, hash_type = 4, 5
    shoff = hash_off + len(symbol_hash)
    dynsym_entries = struct.pack("<IBBHQQ", 0, 0, 0, 0, 0, 0) + b"".join(
        struct.pack("<IBBHQQ", intern(sym), (1 << 4) | 2, 0, 5, code_off, len(code))
        for sym in exports
    )
    dynamic_tags = [(1, intern(lib)) for lib in needed]
    dynamic_tags.extend(
        (
            (5, dynstr_off),
            (10, len(dynstr)),
            (6, dynsym_off),
            (11, 24),
            (hash_tag, hash_off),
        )
    )
    if glibc_versions:
        dynamic_tags.extend(((0x6FFFFFFE, verneed_off), (0x6FFFFFFF, 1)))
    dyn_entries = b"".join(
        struct.pack("<qQ", tag, value) for tag, value in (*dynamic_tags, (0, 0))
    )

    # Section 0: NULL, 1: .dynstr (SHT_STRTAB=3), 2: .dynamic (6), 3: .dynsym (11), 4: .gnu.version_r (0x6ffffffe)
    shdrs = [
        struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        struct.pack(
            "<IIQQQQIIQQ", 0, 3, 2, dynstr_off, dynstr_off, len(dynstr), 0, 0, 1, 0
        ),
        struct.pack(
            "<IIQQQQIIQQ", 0, 6, 3, dyn_off, dyn_off, len(dyn_entries), 1, 0, 8, 16
        ),
        struct.pack(
            "<IIQQQQIIQQ",
            0,
            11,
            2,
            dynsym_off,
            dynsym_off,
            len(dynsym_entries),
            1,
            1,
            8,
            24,
        ),
        struct.pack(
            "<IIQQQQIIQQ",
            0,
            0x6FFFFFFE,
            2,
            verneed_off,
            verneed_off,
            len(verneed_bytes),
            1,
            1 if glibc_versions else 0,
            4,
            0,
        ),
        struct.pack("<IIQQQQIIQQ", 0, 1, 6, code_off, code_off, len(code), 0, 0, 1, 0),
        struct.pack(
            "<IIQQQQIIQQ",
            0,
            hash_type,
            2,
            hash_off,
            hash_off,
            len(symbol_hash),
            3,
            0,
            8,
            0,
        ),
    ]

    ehdr = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01" + b"\x00" * 9,
        3,
        machine,
        1,
        0,
        phoff,
        shoff,
        0,
        ehdr_size,
        phdr_size,
        2,
        shdr_size,
        len(shdrs),
        0,
    )
    phdr = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, shoff, shoff, pt_load_align)
    dynamic_phdr = struct.pack(
        "<IIQQQQQQ",
        2,
        6,
        dyn_off,
        dyn_off,
        dyn_off,
        len(dyn_entries),
        len(dyn_entries),
        8,
    )
    path.write_bytes(
        ehdr
        + phdr
        + dynamic_phdr
        + dynstr
        + dyn_entries
        + dynsym_entries
        + verneed_bytes
        + code
        + symbol_hash
        + b"".join(shdrs)
    )


def _build_synthetic_pe64(
    path: Path,
    *,
    exports: tuple[str, ...],
    imports: tuple[str, ...],
    delay_imports: tuple[str, ...] = (),
    ordinal_only_exports: int = 0,
) -> None:
    dos = bytearray(0x80)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)

    # Single section .rdata at RVA 0x1000, raw offset 0x400
    sec_rva = 0x1000
    sec_raw = 0x400

    rdata = bytearray()
    # Export directory at RVA 0x1000 (40 bytes)
    exp_dir_off = 0
    rdata.extend(b"\x00" * 40)
    function_stub_rva = sec_rva + len(rdata)
    rdata.extend(b"\xc3")
    functions_off = len(rdata)
    rdata.extend(
        struct.pack("<I", function_stub_rva) * (len(exports) + ordinal_only_exports)
    )
    ordinals_off = len(rdata)
    rdata.extend(b"".join(struct.pack("<H", index) for index in range(len(exports))))
    name_rvas_off = len(rdata)
    rdata.extend(b"\x00" * (4 * len(exports)))
    export_name_rvas: list[int] = []
    for sym in exports:
        export_name_rvas.append(sec_rva + len(rdata))
        rdata.extend(sym.encode("utf-8") + b"\x00")
    for idx, rva in enumerate(export_name_rvas):
        struct.pack_into("<I", rdata, name_rvas_off + idx * 4, rva)
    struct.pack_into(
        "<IIHHIIIIIII",
        rdata,
        exp_dir_off,
        0,
        0,
        0,
        0,
        0,
        1,
        len(exports) + ordinal_only_exports,
        len(exports),
        sec_rva + functions_off,
        sec_rva + name_rvas_off,
        sec_rva + ordinals_off,
    )

    # Import descriptors
    imp_dir_off = len(rdata)
    rdata.extend(b"\x00" * (20 * (len(imports) + 1)))
    for idx, dll in enumerate(imports):
        dll_rva = sec_rva + len(rdata)
        rdata.extend(dll.encode("utf-8") + b"\x00")
        struct.pack_into("<IIIII", rdata, imp_dir_off + idx * 20, 0, 0, 0, dll_rva, 0)

    delay_dir_off = len(rdata)
    rdata.extend(b"\x00" * (32 * (len(delay_imports) + 1)))
    for idx, dll in enumerate(delay_imports):
        dll_rva = sec_rva + len(rdata)
        rdata.extend(dll.encode("utf-8") + b"\x00")
        struct.pack_into(
            "<IIIIIIII", rdata, delay_dir_off + idx * 32, 1, dll_rva, 0, 0, 0, 0, 0, 0
        )

    pe_sig = b"PE\x00\x00"
    size_opt = 240
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, size_opt, 0x2000)
    opt = bytearray(size_opt)
    struct.pack_into("<H", opt, 0, 0x20B)
    struct.pack_into("<I", opt, 108, 16)
    struct.pack_into("<II", opt, 112, sec_rva + exp_dir_off, 40)
    struct.pack_into("<II", opt, 120, sec_rva + imp_dir_off, 20 * (len(imports) + 1))
    struct.pack_into(
        "<II", opt, 112 + 13 * 8, sec_rva + delay_dir_off, 32 * (len(delay_imports) + 1)
    )

    sec_hdr = bytearray(40)
    sec_hdr[:6] = b".rdata"
    struct.pack_into("<IIII", sec_hdr, 8, len(rdata), sec_rva, len(rdata), sec_raw)
    struct.pack_into("<I", sec_hdr, 36, 0x60000020)

    headers = bytes(dos) + pe_sig + coff + bytes(opt) + bytes(sec_hdr)
    padding = b"\x00" * (sec_raw - len(headers))
    path.write_bytes(headers + padding + bytes(rdata))


class VerifyBinaryTests(unittest.TestCase):
    def test_android_elf_gates(self) -> None:
        exports = (*sorted(REQUIRED_MPV_EXPORTS), "av_jni_set_java_vm")
        with tempfile.TemporaryDirectory() as td:
            so = Path(td) / "libmpv.so"
            _build_synthetic_elf64(
                so,
                machine=183,
                pt_load_align=0x4000,
                needed=("libc.so", "libEGL.so", "libmediandk.so"),
                exports=exports,
            )
            rep = verify_library_file("android-arm64-v8a", so)
            self.assertEqual(rep["exportCount"], len(exports))

            # 4 KiB alignment must fail
            _build_synthetic_elf64(
                so,
                machine=183,
                pt_load_align=0x1000,
                needed=("libc.so",),
                exports=exports,
            )
            with self.assertRaises(VerificationError):
                verify_library_file("android-arm64-v8a", so)

            # Missing av_jni_set_java_vm must fail
            _build_synthetic_elf64(
                so,
                machine=183,
                pt_load_align=0x4000,
                needed=("libc.so",),
                exports=tuple(sorted(REQUIRED_MPV_EXPORTS)),
            )
            with self.assertRaises(VerificationError):
                verify_library_file("android-arm64-v8a", so)

    def test_linux_glibc_and_symbol_leak_gates(self) -> None:
        exports = tuple(sorted(REQUIRED_MPV_EXPORTS))
        with tempfile.TemporaryDirectory() as td:
            so = Path(td) / "libmpv.so.2"
            _build_synthetic_elf64(
                so,
                machine=62,
                needed=("libc.so.6", "libGL.so.1"),
                exports=exports,
                glibc_versions=("GLIBC_2.17", "GLIBC_2.35"),
            )
            rep = verify_library_file("linux-x64", so)
            self.assertEqual(rep["glibcMax"], "GLIBC_2.35")

            # GLIBC_2.36 must fail
            _build_synthetic_elf64(
                so,
                machine=62,
                needed=("libc.so.6",),
                exports=exports,
                glibc_versions=("GLIBC_2.36",),
            )
            with self.assertRaises(VerificationError):
                verify_library_file("linux-x64", so)

            # Disallowed DT_NEEDED must fail
            _build_synthetic_elf64(
                so,
                machine=62,
                needed=("libc.so.6", "libstdc++.so.6"),
                exports=exports,
                glibc_versions=("GLIBC_2.35",),
            )
            with self.assertRaises(VerificationError):
                verify_library_file("linux-x64", so)

            # Leaked symbol must fail
            _build_synthetic_elf64(
                so,
                machine=62,
                needed=("libc.so.6",),
                exports=(*exports, "avcodec_version"),
                glibc_versions=("GLIBC_2.35",),
            )
            with self.assertRaises(VerificationError):
                verify_library_file("linux-x64", so)

    def test_windows_pe_dll_allowlist(self) -> None:
        exports = tuple(sorted(REQUIRED_MPV_EXPORTS))
        with tempfile.TemporaryDirectory() as td:
            dll = Path(td) / "libmpv-2.dll"
            _build_synthetic_pe64(
                dll,
                exports=exports,
                imports=(
                    "KERNEL32.dll",
                    "dwrite.dll",
                    "api-ms-win-crt-runtime-l1-1-0.dll",
                ),
            )
            rep = verify_library_file("windows-x64", dll)
            self.assertEqual(rep["neededCount"], 3)

            # Disallowed MinGW runtime DLL must fail
            _build_synthetic_pe64(
                dll, exports=exports, imports=("KERNEL32.dll", "libwinpthread-1.dll")
            )
            with self.assertRaises(VerificationError):
                verify_library_file("windows-x64", dll)

    def test_windows_delay_imports_use_the_same_system_dll_gate(self) -> None:
        exports = tuple(sorted(REQUIRED_MPV_EXPORTS))
        with tempfile.TemporaryDirectory() as td:
            dll = Path(td) / "libmpv-2.dll"
            for imported, allowed in (
                ("dwrite.dll", True),
                ("libwinpthread-1.dll", False),
                ("api-ms-win-core-runtime-l1-1-0.so", False),
            ):
                with self.subTest(imported=imported):
                    _build_synthetic_pe64(
                        dll, exports=exports, imports=(), delay_imports=(imported,)
                    )
                    if allowed:
                        self.assertEqual(
                            verify_library_file("windows-x64", dll)["neededCount"], 1
                        )
                    else:
                        with self.assertRaises(VerificationError):
                            verify_library_file("windows-x64", dll)

    def test_windows_invalid_imports_and_phantom_exports_fail_closed(self) -> None:
        exports = tuple(sorted(REQUIRED_MPV_EXPORTS))
        with tempfile.TemporaryDirectory() as td:
            dll = Path(td) / "libmpv-2.dll"
            for corruption in (
                "import_name_rva",
                "missing_import_terminator",
                "export_function",
                "export_ordinal",
                "optional_header_size",
                "section_data_size",
                "non_executable_exports",
                "export_in_headers",
            ):
                with self.subTest(corruption=corruption):
                    _build_synthetic_pe64(
                        dll, exports=exports, imports=("KERNEL32.dll",)
                    )
                    data = bytearray(dll.read_bytes())
                    optional_offset = 0x80 + 24
                    export_offset = 0x400
                    if corruption == "import_name_rva":
                        import_rva = struct.unpack_from(
                            "<I", data, optional_offset + 120
                        )[0]
                        struct.pack_into(
                            "<I", data, 0x400 + import_rva - 0x1000 + 12, 0xFFFF0000
                        )
                    elif corruption == "missing_import_terminator":
                        struct.pack_into("<I", data, optional_offset + 124, 20)
                    elif corruption == "export_function":
                        functions_rva = struct.unpack_from(
                            "<I", data, export_offset + 28
                        )[0]
                        struct.pack_into("<I", data, 0x400 + functions_rva - 0x1000, 0)
                    elif corruption == "export_ordinal":
                        ordinals_rva = struct.unpack_from(
                            "<I", data, export_offset + 36
                        )[0]
                        struct.pack_into(
                            "<H", data, 0x400 + ordinals_rva - 0x1000, len(exports)
                        )
                    elif corruption == "optional_header_size":
                        struct.pack_into("<H", data, 0x80 + 20, 112)
                    elif corruption == "section_data_size":
                        struct.pack_into(
                            "<I", data, optional_offset + 240 + 16, len(data)
                        )
                    elif corruption == "non_executable_exports":
                        struct.pack_into(
                            "<I", data, optional_offset + 240 + 36, 0x40000040
                        )
                    else:
                        functions_rva = struct.unpack_from(
                            "<I", data, export_offset + 28
                        )[0]
                        struct.pack_into(
                            "<I", data, 0x400 + functions_rva - 0x1000, 0x40
                        )
                    dll.write_bytes(data)
                    with self.assertRaises(VerificationError):
                        verify_library_file("windows-x64", dll)

    def test_elf_protected_exports_are_checked_for_symbol_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            so = Path(td) / "libmpv.so.2"
            _build_synthetic_elf64(
                so,
                machine=62,
                exports=(*sorted(REQUIRED_MPV_EXPORTS), "avcodec_version"),
            )
            data = bytearray(so.read_bytes())
            section_headers = struct.unpack_from("<Q", data, 40)[0]
            symbols_offset, symbols_size = struct.unpack_from(
                "<QQ", data, section_headers + 3 * 64 + 24
            )
            data[symbols_offset + symbols_size - 24 + 5] = (
                3  # STV_PROTECTED is externally visible
            )
            so.write_bytes(data)
            with self.assertRaisesRegex(VerificationError, "avcodec_version"):
                verify_library_file("linux-x64", so)

    def test_elf_required_exports_must_resolve_to_mapped_functions(self) -> None:
        exports = (*sorted(REQUIRED_MPV_EXPORTS), "av_jni_set_java_vm")
        with tempfile.TemporaryDirectory() as td:
            so = Path(td) / "libmpv.so"
            for corruption in (
                "object",
                "undefined_section",
                "absolute",
                "null_address",
                "unmapped_address",
                "oversized_function",
                "non_executable_segment",
            ):
                with self.subTest(corruption=corruption):
                    _build_synthetic_elf64(so, machine=183, exports=exports)
                    data = bytearray(so.read_bytes())
                    section_headers = struct.unpack_from("<Q", data, 40)[0]
                    symbol_offset = (
                        struct.unpack_from("<Q", data, section_headers + 3 * 64 + 24)[0]
                        + 24
                    )
                    if corruption == "object":
                        data[symbol_offset + 4] = 0x11
                    elif corruption in ("undefined_section", "absolute"):
                        struct.pack_into(
                            "<H",
                            data,
                            symbol_offset + 6,
                            0xFFFF if corruption == "undefined_section" else 0xFFF1,
                        )
                    elif corruption in ("null_address", "unmapped_address"):
                        struct.pack_into(
                            "<Q",
                            data,
                            symbol_offset + 8,
                            0 if corruption == "null_address" else 0xFFFFFFFFFFFF0000,
                        )
                    elif corruption == "oversized_function":
                        struct.pack_into("<Q", data, symbol_offset + 16, len(data))
                    else:
                        struct.pack_into("<I", data, 64 + 4, 4)
                    so.write_bytes(data)
                    with self.assertRaisesRegex(VerificationError, "mapped function"):
                        verify_library_file("android-arm64-v8a", so)

    def test_elf_filter_dependencies_cannot_bypass_the_system_library_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            so = Path(td) / "libmpv.so.2"
            for dynamic_tag in (0x7FFFFFFD, 0x7FFFFFFF, 0x6FFFFEFB, 0x6FFFFEFC):
                with self.subTest(dynamic_tag=dynamic_tag):
                    _build_synthetic_elf64(
                        so,
                        machine=62,
                        exports=tuple(sorted(REQUIRED_MPV_EXPORTS)),
                        needed=("libstdc++.so.6",),
                    )
                    data = bytearray(so.read_bytes())
                    section_headers = struct.unpack_from("<Q", data, 40)[0]
                    dynamic_offset = struct.unpack_from(
                        "<Q", data, section_headers + 2 * 64 + 24
                    )[0]
                    struct.pack_into("<q", data, dynamic_offset, dynamic_tag)
                    so.write_bytes(data)
                    with self.assertRaisesRegex(
                        VerificationError, "filter or audit dependencies"
                    ):
                        verify_library_file("linux-x64", so)

    def test_windows_unnamed_ordinal_exports_cannot_bypass_the_symbol_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            dll = Path(td) / "libmpv-2.dll"
            _build_synthetic_pe64(
                dll,
                exports=tuple(sorted(REQUIRED_MPV_EXPORTS)),
                imports=(),
                ordinal_only_exports=1,
            )
            with self.assertRaisesRegex(VerificationError, "Unnamed PE ordinal"):
                verify_library_file("windows-x64", dll)

    def test_elf_runtime_hash_tables_must_expose_required_apis(self) -> None:
        exports = tuple(sorted(REQUIRED_MPV_EXPORTS))
        cases = {
            "gnu": (
                "empty_bloom",
                "empty_bucket",
                "bad_chain_hash",
                "invalid_symbol_start",
                "out_of_range_bucket",
            ),
            "sysv": (
                "empty_bucket",
                "wrong_chain_count",
                "cyclic_chain",
                "out_of_range_bucket",
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            so = Path(td) / "libmpv.so.2"
            for style, corruptions in cases.items():
                _build_synthetic_elf64(
                    so, machine=62, exports=exports, hash_style=style
                )
                self.assertEqual(
                    verify_library_file("linux-x64", so)["exportCount"], len(exports)
                )
                for corruption in corruptions:
                    with self.subTest(style=style, corruption=corruption):
                        _build_synthetic_elf64(
                            so, machine=62, exports=exports, hash_style=style
                        )
                        data = bytearray(so.read_bytes())
                        section_headers = struct.unpack_from("<Q", data, 40)[0]
                        hash_offset = struct.unpack_from(
                            "<Q", data, section_headers + 6 * 64 + 24
                        )[0]
                        if style == "gnu":
                            offsets_and_values = {
                                "empty_bloom": (16, "<Q", 0),
                                "empty_bucket": (24, "<I", 0),
                                "bad_chain_hash": (28, "<I", 1),
                                "invalid_symbol_start": (4, "<I", len(exports) + 2),
                                "out_of_range_bucket": (24, "<I", len(exports) + 2),
                            }
                        else:
                            offsets_and_values = {
                                "empty_bucket": (8, "<I", 0),
                                "wrong_chain_count": (4, "<I", len(exports)),
                                "cyclic_chain": (16, "<I", 1),
                                "out_of_range_bucket": (8, "<I", len(exports) + 2),
                            }
                        offset, fmt, value = offsets_and_values[corruption]
                        struct.pack_into(fmt, data, hash_offset + offset, value)
                        so.write_bytes(data)
                        with self.assertRaisesRegex(VerificationError, "symbol hash"):
                            verify_library_file("linux-x64", so)

    def test_invalid_elf_headers_segments_and_versions_fail_closed(self) -> None:
        exports = (*sorted(REQUIRED_MPV_EXPORTS), "av_jni_set_java_vm")
        with tempfile.TemporaryDirectory() as td:
            so = Path(td) / "libmpv.so"
            for corruption in (
                "executable",
                "segment_congruence",
                "non_power_of_two_alignment",
                "program_table",
                "section_table",
                "version_chain",
                "runtime_dynamic_mapping",
                "runtime_string_mapping",
                "runtime_version_mapping",
                "version_aux_chain_tail",
                "version_requirement_chain_tail",
            ):
                with self.subTest(corruption=corruption):
                    _build_synthetic_elf64(
                        so, machine=183, exports=exports, glibc_versions=("GLIBC_2.35",)
                    )
                    data = bytearray(so.read_bytes())
                    if corruption == "executable":
                        struct.pack_into("<H", data, 16, 2)
                    elif corruption == "segment_congruence":
                        struct.pack_into("<Q", data, 64 + 16, 1)
                    elif corruption == "non_power_of_two_alignment":
                        struct.pack_into("<Q", data, 64 + 48, 0xC000)
                    elif corruption == "program_table":
                        struct.pack_into("<Q", data, 32, len(data))
                    elif corruption == "section_table":
                        struct.pack_into("<Q", data, 40, len(data))
                    elif corruption.startswith("runtime_"):
                        sections_offset = struct.unpack_from("<Q", data, 40)[0]
                        section_index = {
                            "runtime_dynamic_mapping": 2,
                            "runtime_string_mapping": 1,
                            "runtime_version_mapping": 4,
                        }[corruption]
                        section_header = sections_offset + section_index * 64
                        address = struct.unpack_from("<Q", data, section_header + 16)[0]
                        struct.pack_into("<Q", data, section_header + 16, address + 1)
                    else:
                        sections_offset = struct.unpack_from("<Q", data, 40)[0]
                        version_section = sections_offset + 4 * 64
                        version_offset = struct.unpack_from(
                            "<Q", data, version_section + 24
                        )[0]
                        offset, value = {
                            "version_chain": (8, 0xFFFF),
                            "version_aux_chain_tail": (16 + 12, 16),
                            "version_requirement_chain_tail": (12, 16),
                        }[corruption]
                        struct.pack_into("<I", data, version_offset + offset, value)
                    so.write_bytes(data)
                    with self.assertRaises(VerificationError):
                        verify_library_file("android-arm64-v8a", so)

    def test_header_parity_requires_both_directories_and_public_headers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            built, repo = root / "built", root / "repo"
            built.mkdir()
            repo.mkdir()
            self.assertTrue(verify_headers_parity(built, repo))
            with self.assertRaisesRegex(VerificationError, "Both"):
                verify_library_file(
                    "linux-x64", root / "unused", built_headers_dir=built
                )
            for name in ("client.h", "render.h", "stream_cb.h"):
                (built / name).write_text(name)
                (repo / name).write_text(name)
            self.assertEqual(verify_headers_parity(built, repo), [])
            (repo / "client.h").write_text("outdated")
            self.assertIn("client.h", " ".join(verify_headers_parity(built, repo)))

    def test_smoke_uses_exact_library_disables_config_and_has_a_deadline(self) -> None:
        with patch("libmpv_build.verify.subprocess.run") as runner:
            runner.return_value = subprocess.CompletedProcess([], 0, "", "")
            run_linux_smoke(Path("libmpv.so.2"))
            args, kwargs = runner.call_args
            script = args[0][2]
            self.assertIn(repr(str(Path("libmpv.so.2").resolve())), script)
            self.assertIn("b'config', b'no'", script)
            self.assertIn("b'load-scripts', b'no'", script)
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
            with patch("ctypes.CDLL") as load_library:
                load_library.return_value.mpv_client_api_version.return_value = 0
                with self.assertRaisesRegex(RuntimeError, "bad api version"):
                    exec(compile(script, "<smoke>", "exec", optimize=2), {})  # noqa: S102 - Exercise the generated smoke script with optimized bytecode
            runner.side_effect = subprocess.TimeoutExpired([], 30)
            with self.assertRaisesRegex(VerificationError, "timed out"):
                run_linux_smoke(Path("libmpv.so.2"))

    def test_delivery_zip_rejects_symlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive_path = Path(td) / "delivery.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                info = zipfile.ZipInfo("libmpv.so.2")
                info.external_attr = 0o120777 << 16
                archive.writestr(info, "other.so")
            with self.assertRaisesRegex(VerificationError, "flat"):
                verify_path("linux-x64", archive_path)


if __name__ == "__main__":
    unittest.main()
