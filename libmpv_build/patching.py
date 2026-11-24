"""Fail-fast source and pkg-config patching helpers."""

from __future__ import annotations

import re
from pathlib import Path


class PatchError(RuntimeError):
    """Raised when an expected upstream source anchor is missing or ambiguous."""


def replace_exact(path: Path, old: str, new: str, *, expected_count: int = 1) -> None:
    content = path.read_text(encoding="utf-8")
    actual = content.count(old)
    if actual != expected_count:
        raise PatchError(
            f"Patch anchor mismatch in {path}: expected {expected_count} occurrence(s) of {old!r}, found {actual}"
        )
    path.write_text(content.replace(old, new, expected_count), encoding="utf-8")


def _meson_code_mask(text: str) -> str:
    """Hide strings and comments while keeping code offsets for dependency parsing."""
    masked = list(text)
    index = 0
    while index < len(text):
        if text[index] == "#":
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
        elif text[index] in ("'", '"'):
            delimiter = text[index] * (
                3 if text.startswith(text[index] * 3, index) else 1
            )
            end = index + len(delimiter)
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                elif text.startswith(delimiter, end):
                    end += len(delimiter)
                    break
                else:
                    end += 1
            else:
                raise PatchError("Unterminated Meson string")
        else:
            index += 1
            continue
        masked[index:end] = ["\n" if char == "\n" else " " for char in text[index:end]]
        index = end
    return "".join(masked)


def enforce_static_meson_dependencies(
    source_dir: Path, static_packages: tuple[str, ...]
) -> int:
    """Require literal static linking for selected dependencies, rejecting conflicting upstream changes."""
    updated_files = 0
    for path in sorted(source_dir.rglob("meson.build")):
        original = path.read_text(encoding="utf-8")
        code = _meson_code_mask(original)
        insertions: list[int] = []
        for call in re.finditer(r"\bdependency\s*\(", code):
            argument = re.match(r"\s*(['\"])([^'\"]+)\1", original[call.end() :])
            if argument is None or argument.group(2) not in static_packages:
                continue
            arguments_start = call.end() + argument.end()
            depth = 1
            closing: int | None = None
            top_level_arguments: list[tuple[int, int]] = []
            start = arguments_start
            for index in range(arguments_start, len(code)):
                char = code[index]
                if char in "([{":
                    depth += 1
                elif char in ")]}":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        top_level_arguments.append((start, index))
                        break
                elif char == "," and depth == 1:
                    top_level_arguments.append((start, index))
                    start = index + 1
            if closing is None:
                raise PatchError(
                    f"Unterminated Meson dependency in {path}: {argument.group(2)}"
                )
            static_arguments = []
            for begin, end in top_level_arguments:
                keyword = re.match(r"\s*static\s*:\s*", code[begin:end])
                if keyword is not None:
                    static_arguments.append(code[begin + keyword.end() : end].strip())
            if static_arguments and static_arguments != ["true"]:
                raise PatchError(
                    f"Dependency must use literal static: true in {path}: {argument.group(2)}"
                )
            if not static_arguments:
                insertions.append(arguments_start)
        text = original
        for index in reversed(insertions):
            text = text[:index] + ", static: true" + text[index:]
        if text != original:
            path.write_text(text, encoding="utf-8")
            updated_files += 1
    return updated_files


def patch_mpv_meson(
    source_dir: Path, *, final_link_args: list[str], enable_cpp_link: bool
) -> None:
    meson_build = source_dir / "meson.build"
    if enable_cpp_link:
        replace_exact(
            meson_build,
            "libmpv = library('mpv', sources,",
            "add_languages('cpp', native: false)\nlibmpv = library('mpv', sources, link_language: 'cpp',",
            expected_count=1,
        )
    if final_link_args:
        original = "link_args: cc.get_supported_link_arguments(['-Wl,-Bsymbolic']),"
        replacement = f"link_args: cc.get_supported_link_arguments(['-Wl,-Bsymbolic']) + {final_link_args!r},"
        replace_exact(meson_build, original, replacement, expected_count=1)


def rewrite_linux_pc_system_libs(
    pkgconfig_dir: Path, system_libraries: frozenset[str]
) -> None:
    """Resolve system -l flags to explicit shared library paths inside an assembled prefix."""
    names = {
        match.group(1)
        for item in system_libraries
        if (match := re.fullmatch(r"lib(.+)\.so(?:\.\d+)*", item))
    }
    if not names:
        return
    pattern = re.compile(
        r"(?<!\S)-l(" + "|".join(re.escape(name) for name in sorted(names)) + r")(?!\S)"
    )
    sys_libdir = Path("/usr/lib/x86_64-linux-gnu")

    def resolve_shared(match: re.Match[str]) -> str:
        lib_base = match.group(1)
        library = sys_libdir / f"lib{lib_base}.so"
        if not library.is_file():
            sonames = sorted(
                name
                for name in system_libraries
                if name.startswith(f"lib{lib_base}.so.")
            )
            candidates = [sys_libdir / soname for soname in sonames]
            library = next((cand for cand in candidates if cand.is_file()), library)
        if not library.is_file():
            raise RuntimeError(f"System development library is missing: {library}")
        return str(library)

    for pc in sorted(pkgconfig_dir.glob("*.pc")):
        pc.write_text(
            pattern.sub(resolve_shared, pc.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
