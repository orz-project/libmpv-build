"""Typed lockfile, target profiles, and platform allowlists."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from . import ROOT

TARGETS = ("android-arm64-v8a", "android-x86_64", "windows-x64", "linux-x64")

STATIC_PACKAGES = (
    "libavcodec",
    "libavfilter",
    "libavformat",
    "libavutil",
    "libswresample",
    "libswscale",
    "libass",
    "libplacebo",
    "freetype2",
    "harfbuzz",
    "fribidi",
    "libunibreak",
    "fontconfig",
    "libxml-2.0",
    "zlib",
    "dav1d",
    "shaderc",
    "shaderc_combined",
    "spirv-cross-c-shared",
    "spirv-cross-c",
)

FFMPEG_FILTERS = (
    "buffer",
    "buffersink",
    "abuffer",
    "abuffersink",
    "format",
    "aformat",
    "null",
    "anull",
    "copy",
    "acopy",
    "aresample",
    "scale",
    "crop",
    "pad",
    "transpose",
    "hflip",
    "vflip",
    "rotate",
    "fps",
    "setpts",
    "yadif",
    "bwdif",
    "eq",
    "deband",
    "volume",
    "atempo",
    "pan",
    "channelmap",
    "channelsplit",
    "amerge",
    "amix",
    "loudnorm",
    "dynaudnorm",
)

REQUIRED_MPV_EXPORTS = frozenset(
    {
        "mpv_client_api_version",
        "mpv_create",
        "mpv_initialize",
        "mpv_destroy",
        "mpv_terminate_destroy",
        "mpv_command",
        "mpv_command_async",
        "mpv_command_string",
        "mpv_set_option",
        "mpv_set_option_string",
        "mpv_set_property",
        "mpv_set_property_string",
        "mpv_get_property",
        "mpv_get_property_string",
        "mpv_observe_property",
        "mpv_unobserve_property",
        "mpv_wait_event",
        "mpv_wakeup",
        "mpv_render_context_create",
        "mpv_render_context_render",
        "mpv_render_context_update",
        "mpv_render_context_free",
        "mpv_stream_cb_add_ro",
        "mpv_free",
        "mpv_free_node_contents",
    }
)

LINUX_SYSTEM_LIBRARIES = frozenset(
    {
        "libc.so.6",
        "libm.so.6",
        "libdl.so.2",
        "libpthread.so.0",
        "librt.so.1",
        "ld-linux-x86-64.so.2",
        "libGL.so.1",
        "libEGL.so.1",
        "libGLESv2.so.2",
        "libOpenGL.so.0",
        "libGLX.so.0",
        "libX11.so.6",
        "libXext.so.6",
        "libXrandr.so.2",
        "libXpresent.so.1",
        "libXss.so.1",
        "libXfixes.so.3",
        "libXinerama.so.1",
        "libXcursor.so.1",
        "libxcb.so.1",
        "libdrm.so.2",
        "libva.so.2",
        "libva-drm.so.2",
        "libva-x11.so.2",
        "libvdpau.so.1",
        "libpulse.so.0",
        "libasound.so.2",
    }
)

MAX_GLIBC_VERSION = (2, 35)

ANDROID_SYSTEM_LIBRARIES = frozenset(
    {
        "libc.so",
        "libm.so",
        "libdl.so",
        "liblog.so",
        "libandroid.so",
        "libmediandk.so",
        "libEGL.so",
        "libGLESv2.so",
        "libGLESv3.so",
        "libjnigraphics.so",
    }
)

WINDOWS_SYSTEM_DLLS = frozenset(
    {
        "kernel32.dll",
        "user32.dll",
        "gdi32.dll",
        "advapi32.dll",
        "shell32.dll",
        "ole32.dll",
        "oleaut32.dll",
        "ws2_32.dll",
        "winmm.dll",
        "bcrypt.dll",
        "d3d11.dll",
        "dxgi.dll",
        "opengl32.dll",
        "dwrite.dll",
        "avrt.dll",
        "dwmapi.dll",
        "imm32.dll",
        "ntdll.dll",
        "shcore.dll",
        "uxtheme.dll",
        "shlwapi.dll",
        "version.dll",
        "ucrtbase.dll",
    }
)

WINDOWS_SYSTEM_DLL_PREFIXES = (
    "api-ms-win-crt-",
    "api-ms-win-core-",
)

RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class TargetProfile:
    name: str
    system: str
    cpu: str
    triple: str
    library: str
    libass: tuple[str, ...]
    mpv: tuple[str, ...]
    ffmpeg: tuple[str, ...]
    api: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "system": self.system,
            "cpu": self.cpu,
            "triple": self.triple,
            "library": self.library,
            "libass": list(self.libass),
            "mpv": list(self.mpv),
            "ffmpeg": list(self.ffmpeg),
        }
        if self.api is not None:
            data["api"] = self.api
        return data


@dataclass(frozen=True)
class DependencySpec:
    name: str
    repository: str
    targets: tuple[str, ...]
    tag: str | None
    version: str
    commit: str
    date: str
    sha256: str
    tag_pattern: str | None
    pin_from: dict[str, Any] | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ToolchainSpec:
    system: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class LockFile:
    release: str
    repository: str
    toolchains: dict[str, ToolchainSpec]
    dependencies: dict[str, DependencySpec]
    build_tools: dict[str, Any]
    raw: dict[str, Any]

    def dependency_dicts_for_target(self, target: str) -> dict[str, dict[str, Any]]:
        return {
            name: dict(spec.raw)
            for name, spec in self.dependencies.items()
            if target in spec.targets
        }


def validate_https_url(url: str, context: str) -> None:
    if not isinstance(url, str) or any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url
    ):
        raise ValueError(f"{context} must use an HTTPS URL")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{context} must use an HTTPS URL with a valid host")


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")  # noqa: TRY004 - Invalid JSON values use ValueError
    return value


def _text(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ValueError(f"{context} must be a nonempty single-line string")
    return value


def _digest(value: Any, context: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{context} must be a SHA-256 digest")


def _validate_pins(dependencies: dict[str, DependencySpec]) -> None:
    paths: dict[str, list[str]] = {}
    for name, spec in dependencies.items():
        pin = spec.pin_from
        if pin is None:
            continue
        parent = pin.get("parent")
        if not isinstance(parent, str) or parent not in dependencies or parent == name:
            raise ValueError(f"Invalid pinned dependency parent: {name}")
        if not set(spec.targets).issubset(dependencies[parent].targets):
            raise ValueError(f"Pinned dependency targets exceed parent targets: {name}")
        if pin.get("type") == "gitlink":
            value = _text(pin.get("path"), f"Submodule path for {name}")
            path = PurePosixPath(value)
            reserved = {".complete", ".complete.json", ".libmpv-source.json"}
            if (
                path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or "\\" in value
                or ":" in value
                or reserved.intersection(path.parts)
            ):
                raise ValueError(f"Unsafe submodule path: {value}")
            normalized = path.as_posix()
            previous = paths.setdefault(parent, [])
            if any(
                normalized == old
                or normalized.startswith(old + "/")
                or old.startswith(normalized + "/")
                for old in previous
            ):
                raise ValueError(f"Overlapping submodule paths in {parent}")
            previous.append(normalized)
        elif pin.get("type") == "deps":
            variable = pin.get("variable")
            if not isinstance(variable, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", variable
            ):
                raise ValueError(f"Invalid DEPS variable for {name}")
        else:
            raise ValueError(f"Unsupported pin type for {name}")
    for name in dependencies:
        visited: set[str] = set()
        current = name
        while dependencies[current].pin_from is not None:
            if current in visited:
                raise ValueError(f"Cyclic pinned dependencies: {name}")
            visited.add(current)
            current = dependencies[current].pin_from["parent"]


def load_lock(path: Path | None = None) -> LockFile:
    lock_path = path or (ROOT / "dependencies.json")
    raw = _object(json.loads(lock_path.read_text(encoding="utf-8")), "Dependency lock")
    release = raw.get("release", "")
    if not isinstance(release, str) or not RELEASE_RE.fullmatch(release):
        raise ValueError("Release must be a safe immutable asset name")
    repository = raw.get("repository")
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError("Repository must be an owner/name identifier")
    toolchains_raw = _object(raw.get("toolchains"), "Toolchains")
    if set(toolchains_raw) != {"android", "windows", "linux"}:
        raise ValueError("Toolchains must define Android, Windows, and Linux")
    toolchains: dict[str, ToolchainSpec] = {}
    for system, value in toolchains_raw.items():
        record = _object(value, f"Toolchain {system}")
        _text(record.get("version"), f"Toolchain {system} version")
        if system in ("android", "windows"):
            validate_https_url(record.get("url"), f"Toolchain {system}")
            _digest(record.get("sha256"), f"Toolchain {system}")
        if system == "android":
            revision = record.get("revision")
            if not isinstance(revision, str) or not re.fullmatch(
                r"[0-9]+(?:\.[0-9]+)+", revision
            ):
                raise ValueError("Android NDK revision must be a numeric revision")
        if system == "linux":
            image = record.get("image")
            if not isinstance(image, str) or not re.fullmatch(
                r"ubuntu:22\.04@sha256:[0-9a-f]{64}", image
            ):
                raise ValueError(
                    "Linux toolchain must pin the Ubuntu 22.04 image digest"
                )
            if record.get("glibc") != "2.35":
                raise ValueError("Linux toolchain must retain the glibc 2.35 baseline")
            _text(record.get("meson"), "Linux Meson version")
            _text(record.get("llvm"), "Linux LLVM version")
        toolchains[system] = ToolchainSpec(system=system, raw=dict(record))
    build_tools = _object(raw.get("buildTools"), "Build tools")
    if "meson" not in build_tools:
        raise ValueError("The Meson build tool must be locked")
    for name, value in build_tools.items():
        record = _object(value, f"Build tool {name}")
        _text(record.get("version"), f"Build tool {name} version")
        validate_https_url(record.get("url"), f"Build tool {name}")
        _digest(record.get("sha256"), f"Build tool {name}")
        if "wheelSha256" in record:
            _digest(record["wheelSha256"], f"Build tool {name} wheel")
    dependencies: dict[str, DependencySpec] = {}
    records = _object(raw.get("dependencies"), "Dependencies")
    if "mpv" not in records:
        raise ValueError("The mpv dependency must be locked")
    for name, value in records.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError(f"Invalid dependency name: {name}")
        record = _object(value, f"Dependency {name}")
        validate_https_url(record.get("repository", ""), f"Dependency {name}")
        commit = record.get("commit", "")
        digest = record.get("sha256", "")
        if (
            not isinstance(commit, str)
            or not COMMIT_RE.fullmatch(commit)
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise ValueError(f"Dependency is not fully locked: {name}")
        target_list = record.get("targets")
        if (
            not isinstance(target_list, list)
            or not target_list
            or any(not isinstance(t, str) or t not in TARGETS for t in target_list)
            or len(set(target_list)) != len(target_list)
        ):
            raise ValueError(f"Dependency {name} has invalid targets")
        targets = tuple(target_list)
        version = _text(record.get("version"), f"Dependency {name} version")
        date = _text(record.get("date"), f"Dependency {name} date")
        try:
            parsed_date = datetime.fromisoformat(date.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Dependency {name} date must be ISO-8601") from None
        if parsed_date.tzinfo is None:
            raise ValueError(f"Dependency {name} date must include a timezone")
        pin = (
            _object(record["pinFrom"], f"Dependency {name} pin")
            if "pinFrom" in record
            else None
        )
        tag = record.get("tag")
        if pin is None:
            _text(tag, f"Dependency {name} tag")
            pattern = _text(record.get("tagPattern"), f"Dependency {name} tag pattern")
            try:
                compiled = re.compile(pattern)
            except re.error:
                raise ValueError(f"Invalid dependency tag pattern: {name}") from None
            if compiled.groups < 1:
                raise ValueError(
                    f"Dependency tag pattern must capture a version: {name}"
                )
        elif tag is not None:
            _text(tag, f"Dependency {name} tag")
        if "tagPattern" in record and not isinstance(record["tagPattern"], str):
            raise ValueError(f"Invalid dependency tag pattern: {name}")
        if not targets:
            raise ValueError(f"Dependency {name} has invalid targets: {targets}")
        dependencies[name] = DependencySpec(
            name=name,
            repository=record["repository"],
            targets=targets,
            tag=record.get("tag"),
            version=version,
            commit=commit,
            date=date,
            sha256=digest,
            tag_pattern=record.get("tagPattern"),
            pin_from=dict(pin) if pin is not None else None,
            raw=dict(record),
        )
    _validate_pins(dependencies)
    return LockFile(
        release=release,
        repository=repository,
        toolchains=toolchains,
        dependencies=dependencies,
        build_tools=dict(build_tools),
        raw=raw,
    )


def load_profiles(path: Path | None = None) -> dict[str, TargetProfile]:
    profile_path = path or (ROOT / "targets.json")
    raw = _object(
        json.loads(profile_path.read_text(encoding="utf-8")), "Target profiles"
    )
    if set(raw) != set(TARGETS):
        raise ValueError("Profiles must define exactly the four supported targets")
    expected = {
        "android-arm64-v8a": (
            "android",
            "aarch64",
            "aarch64-linux-android",
            "libmpv.so",
        ),
        "android-x86_64": ("android", "x86_64", "x86_64-linux-android", "libmpv.so"),
        "windows-x64": ("windows", "x86_64", "x86_64-w64-mingw32", "libmpv-2.dll"),
        "linux-x64": ("linux", "x86_64", "x86_64-linux-gnu", "libmpv.so.2"),
    }
    profiles: dict[str, TargetProfile] = {}
    for target in TARGETS:
        if target not in raw:
            raise ValueError(f"Missing target profile: {target}")
        item = _object(raw[target], f"Profile {target}")
        if (
            tuple(item.get(field) for field in ("system", "cpu", "triple", "library"))
            != expected[target]
        ):
            raise ValueError(
                f"Profile {target} has mismatched platform ABI or library name"
            )
        api = item.get("api")
        if target.startswith("android-"):
            if type(api) is not int or not 21 <= api <= 100:
                raise ValueError(
                    f"Profile {target} requires a valid 64-bit Android API level"
                )
        elif "api" in item:
            raise ValueError(f"Profile {target} must not declare an Android API level")
        for field in ("libass", "mpv", "ffmpeg"):
            options = item.get(field)
            if not isinstance(options, list):
                raise ValueError(f"Profile {target} {field} options must be a list")  # noqa: TRY004 - Invalid JSON values use ValueError
            for option in options:
                _text(option, f"Profile {target} {field} option")
                if field in ("libass", "ffmpeg") and not option.startswith("--"):
                    raise ValueError(
                        f"Profile {target} {field} option must begin with --"
                    )
                if field == "mpv" and not re.fullmatch(r"[A-Za-z0-9_-]+=.+", option):
                    raise ValueError(
                        f"Profile {target} mpv option must be a Meson key=value"
                    )
        profiles[target] = TargetProfile(
            name=target,
            system=item["system"],
            cpu=item["cpu"],
            triple=item["triple"],
            library=item["library"],
            libass=tuple(item["libass"]),
            mpv=tuple(item["mpv"]),
            ffmpeg=tuple(item["ffmpeg"]),
            api=item.get("api"),
        )
    return profiles
