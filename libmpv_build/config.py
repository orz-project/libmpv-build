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
OPT_LEVELS = frozenset({"O0", "O1", "O2", "O3", "Os", "Oz"})
LTO_MODES = frozenset({"full", "thin"})
TIERED_RECIPES = frozenset({
    "zlib-ng", "dav1d", "ffmpeg", "libass", "libplacebo", "mpv", "freetype",
    "fribidi", "harfbuzz", "libunibreak", "libxml2", "fontconfig", "shaderc",
    "spirv-cross", "uchardet", "win-iconv", "libiconv", "wayland", "wayland-protocols",
})
SECTIONED_RECIPES = {
    "fontconfig": {"common", "linux", "android"},
    "libplacebo": {"common", "windows"},
}

RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")

# The configurable gate may require additional APIs, but it must always cover
# the runtime contract used by native adapters
MINIMUM_MPV_EXPORTS = frozenset({
    "mpv_client_api_version", "mpv_command", "mpv_command_async", "mpv_command_ret",
    "mpv_command_string", "mpv_create", "mpv_destroy", "mpv_free",
    "mpv_free_node_contents", "mpv_get_property", "mpv_get_property_string",
    "mpv_initialize", "mpv_observe_property", "mpv_render_context_create",
    "mpv_render_context_free", "mpv_render_context_render",
    "mpv_render_context_set_update_callback", "mpv_render_context_update",
    "mpv_set_option", "mpv_set_option_string", "mpv_set_property",
    "mpv_set_property_string", "mpv_stream_cb_add_ro", "mpv_terminate_destroy",
    "mpv_unobserve_property", "mpv_wait_event", "mpv_wakeup",
})


@dataclass(frozen=True)
class SystemFlags:
    cflags: tuple[str, ...]
    cxxflags: tuple[str, ...]
    ldflags: tuple[str, ...]


@dataclass(frozen=True)
class ToolchainFlagsConfig:
    lto: str
    default_opt: str
    march: dict[str, str]
    common_cflags: tuple[str, ...]
    common_ldflags: tuple[str, ...]
    systems: dict[str, SystemFlags]


@dataclass(frozen=True)
class PatchReplacement:
    file: str
    old: str
    new: str


@dataclass(frozen=True)
class PatchesConfig:
    freetype_disabled_modules: tuple[str, ...]
    mpv_wayland_compat: tuple[PatchReplacement, ...]


@dataclass(frozen=True)
class FfmpegConfig:
    decoders_common: tuple[str, ...]
    decoders_android: tuple[str, ...]
    decoders_desktop: tuple[str, ...]
    demuxers: tuple[str, ...]
    parsers: tuple[str, ...]
    bsfs: tuple[str, ...]
    protocols: tuple[str, ...]
    filters: tuple[str, ...]


@dataclass(frozen=True)
class VerifyPolicyConfig:
    max_glibc_version: tuple[int, int]
    required_mpv_exports: frozenset[str]
    linux_system_libraries: frozenset[str]
    android_system_libraries: frozenset[str]
    windows_system_dlls: frozenset[str]
    windows_system_dll_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class UpdatePinsConfig:
    required_gitlinks: dict[str, frozenset[str]]
    shaderc_deps: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class BuildConfig:
    toolchain: ToolchainFlagsConfig
    optimization_tiers: dict[str, str]
    static_packages: tuple[str, ...]
    recipes: dict[str, tuple[str, ...] | dict[str, tuple[str, ...]]]
    patches: PatchesConfig
    ffmpeg: FfmpegConfig
    verify: VerifyPolicyConfig
    update: UpdatePinsConfig
    raw: dict[str, Any]


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
    repository: str | None
    targets: tuple[str, ...]
    tag: str | None
    version: str
    commit: str | None
    date: str
    sha256: str
    tag_pattern: str | None
    pin_from: dict[str, Any] | None
    raw: dict[str, Any]
    url: str | None = None
    strip_prefix: str | None = None


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
        if dependencies[parent].commit is None:
            raise ValueError(f"Pinned dependency parent must be Git-locked: {name}")
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
    lock_repository = raw.get("repository")
    if not isinstance(lock_repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", lock_repository
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
        has_repo = "repository" in record
        has_url = "url" in record
        if has_repo == has_url:
            raise ValueError(
                f"Dependency {name} must specify exactly one of repository or url"
            )
        digest = record.get("sha256", "")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
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

        dependency_repository: str | None = None
        commit: str | None = None
        url: str | None = None
        strip_prefix: str | None = None
        pin: dict[str, Any] | None = None
        tag = record.get("tag")

        if has_url:
            if name == "mpv":
                raise ValueError("The mpv dependency must be Git-locked")
            for forbidden in ("commit", "pinFrom", "tag", "tagPattern"):
                if forbidden in record:
                    raise ValueError(
                        f"Tarball dependency {name} must not define {forbidden}"
                    )
            url = record.get("url", "")
            validate_https_url(url, f"Dependency {name}")
            if "stripPrefix" not in record:
                raise ValueError(f"Tarball dependency {name} must define stripPrefix")
            strip_prefix = _text(
                record.get("stripPrefix"), f"Dependency {name} stripPrefix"
            )
            if (
                "/" in strip_prefix
                or "\\" in strip_prefix
                or strip_prefix in (".", "..")
            ):
                raise ValueError(f"Unsafe stripPrefix for {name}: {strip_prefix}")
        else:
            dependency_repository = record.get("repository", "")
            validate_https_url(dependency_repository, f"Dependency {name}")
            commit = record.get("commit", "")
            if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
                raise ValueError(f"Dependency is not fully locked: {name}")
            pin = (
                _object(record["pinFrom"], f"Dependency {name} pin")
                if "pinFrom" in record
                else None
            )
            if pin is None:
                _text(tag, f"Dependency {name} tag")
                pattern = _text(
                    record.get("tagPattern"), f"Dependency {name} tag pattern"
                )
                try:
                    compiled = re.compile(pattern)
                except re.error:
                    raise ValueError(
                        f"Invalid dependency tag pattern: {name}"
                    ) from None
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
            repository=dependency_repository,
            targets=targets,
            tag=tag,
            version=version,
            commit=commit,
            date=date,
            sha256=digest,
            tag_pattern=record.get("tagPattern"),
            pin_from=dict(pin) if pin is not None else None,
            raw=dict(record),
            url=url,
            strip_prefix=strip_prefix,
        )
    _validate_pins(dependencies)
    return LockFile(
        release=release,
        repository=lock_repository,
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
            if type(api) is not int or api != 28:
                raise ValueError(
                    f"Profile {target} must retain the Android API 28 baseline"
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


def _text_tuple(value: Any, context: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")  # noqa: TRY004 - Invalid JSON values use ValueError
    if not allow_empty and not value:
        raise ValueError(f"{context} must not be empty")
    return tuple(_text(item, f"{context} item") for item in value)


def load_build_config(path: Path | None = None) -> BuildConfig:
    config_path = path or (ROOT / "build-config.json")
    raw = _object(
        json.loads(config_path.read_text(encoding="utf-8")), "Build configuration"
    )

    tc_raw = _object(raw.get("toolchain"), "Build toolchain configuration")
    lto = _text(tc_raw.get("lto"), "Toolchain lto")
    if lto not in LTO_MODES:
        raise ValueError(f"Unsupported LTO mode: {lto}")
    default_opt = _text(tc_raw.get("defaultOpt"), "Toolchain defaultOpt")
    if default_opt not in OPT_LEVELS:
        raise ValueError(f"Unsupported default optimization level: {default_opt}")
    march_raw = _object(tc_raw.get("march"), "Toolchain march")
    for arch in ("aarch64", "x86_64"):
        if arch not in march_raw:
            raise ValueError(f"Toolchain march is missing CPU architecture: {arch}")
    march = {
        _text(k, "Toolchain march key"): _text(v, f"Toolchain march[{k}]")
        for k, v in march_raw.items()
    }
    common_cflags = _text_tuple(
        tc_raw.get("commonCflags"), "Toolchain commonCflags", allow_empty=True
    )
    common_ldflags = _text_tuple(
        tc_raw.get("commonLdflags"), "Toolchain commonLdflags", allow_empty=True
    )
    systems_raw = _object(tc_raw.get("systems"), "Toolchain systems")
    if set(systems_raw) != {"linux", "android", "windows"}:
        raise ValueError("Toolchain systems must define linux, android, and windows")
    systems: dict[str, SystemFlags] = {}
    for sys_name, sys_val in systems_raw.items():
        sys_obj = _object(sys_val, f"Toolchain systems.{sys_name}")
        systems[sys_name] = SystemFlags(
            cflags=_text_tuple(
                sys_obj.get("cflags"), f"Toolchain {sys_name} cflags", allow_empty=True
            ),
            cxxflags=_text_tuple(
                sys_obj.get("cxxflags"),
                f"Toolchain {sys_name} cxxflags",
                allow_empty=True,
            ),
            ldflags=_text_tuple(
                sys_obj.get("ldflags"), f"Toolchain {sys_name} ldflags", allow_empty=True
            ),
        )
    toolchain_cfg = ToolchainFlagsConfig(
        lto=lto,
        default_opt=default_opt,
        march=march,
        common_cflags=common_cflags,
        common_ldflags=common_ldflags,
        systems=systems,
    )

    tiers_raw = _object(raw.get("optimizationTiers"), "Optimization tiers")
    if not TIERED_RECIPES.issubset(tiers_raw):
        raise ValueError(
            f"Missing optimization tiers: {sorted(TIERED_RECIPES - tiers_raw.keys())}"
        )
    optimization_tiers: dict[str, str] = {}
    for dep_name, level in tiers_raw.items():
        name_text = _text(dep_name, "Optimization tier dependency")
        level_text = _text(level, f"Optimization tier for {name_text}")
        if level_text not in OPT_LEVELS:
            raise ValueError(
                f"Unsupported optimization level for {name_text}: {level_text}"
            )
        optimization_tiers[name_text] = level_text

    static_packages = _text_tuple(raw.get("staticPackages"), "Static packages")

    recipes_raw = _object(raw.get("recipes"), "Recipes configuration")
    required_recipes = TIERED_RECIPES - {"ffmpeg", "libunibreak"}
    if not required_recipes.issubset(recipes_raw):
        raise ValueError(f"Missing recipes: {sorted(required_recipes - recipes_raw.keys())}")
    recipes: dict[str, tuple[str, ...] | dict[str, tuple[str, ...]]] = {}
    for rec_name, rec_val in recipes_raw.items():
        rec_key = _text(rec_name, "Recipe configuration key")
        if rec_key in SECTIONED_RECIPES:
            sections = _object(rec_val, f"Recipe {rec_key}")
            if not SECTIONED_RECIPES[rec_key].issubset(sections):
                raise ValueError(f"Recipe {rec_key} is missing required sections")
        elif rec_key in required_recipes and not isinstance(rec_val, list):
            raise ValueError(f"Recipe {rec_key} options must be a list")
        if isinstance(rec_val, list):
            recipes[rec_key] = _text_tuple(
                rec_val, f"Recipe {rec_key} options", allow_empty=True
            )
        elif isinstance(rec_val, dict):
            recipes[rec_key] = {
                _text(sub_k, f"Recipe {rec_key} section"): _text_tuple(
                    sub_v, f"Recipe {rec_key}.{sub_k}", allow_empty=True
                )
                for sub_k, sub_v in rec_val.items()
            }
        else:
            raise ValueError(f"Recipe {rec_key} must be a list or object")  # noqa: TRY004 - Invalid JSON values use ValueError

    patches_raw = _object(raw.get("patches"), "Patches configuration")
    ft_disabled = _text_tuple(
        patches_raw.get("freetypeDisabledModules"), "patches.freetypeDisabledModules"
    )
    wl_compat_raw = patches_raw.get("mpvWaylandCompat")
    if not isinstance(wl_compat_raw, list):
        raise ValueError("patches.mpvWaylandCompat must be a list")  # noqa: TRY004 - Invalid JSON values use ValueError
    wl_compat: list[PatchReplacement] = []
    for item in wl_compat_raw:
        entry = _object(item, "mpvWaylandCompat entry")
        rel_file = _text(entry.get("file"), "mpvWaylandCompat.file")
        rel_path = PurePosixPath(rel_file)
        if (
            rel_path.is_absolute()
            or not rel_path.parts
            or ".." in rel_path.parts
            or "\\" in rel_file
            or ":" in rel_file
        ):
            raise ValueError(f"Unsafe patch file path: {rel_file}")
        wl_compat.append(
            PatchReplacement(
                file=rel_path.as_posix(),
                old=_text(entry.get("old"), "mpvWaylandCompat.old"),
                new=_text(entry.get("new"), "mpvWaylandCompat.new"),
            )
        )
    patches_cfg = PatchesConfig(
        freetype_disabled_modules=ft_disabled,
        mpv_wayland_compat=tuple(wl_compat),
    )

    ff_raw = _object(raw.get("ffmpeg"), "FFmpeg configuration")
    ff_dec = _object(ff_raw.get("decoders"), "FFmpeg decoders")
    ffmpeg_cfg = FfmpegConfig(
        decoders_common=_text_tuple(ff_dec.get("common"), "ffmpeg.decoders.common"),
        decoders_android=_text_tuple(ff_dec.get("android"), "ffmpeg.decoders.android"),
        decoders_desktop=_text_tuple(ff_dec.get("desktop"), "ffmpeg.decoders.desktop"),
        demuxers=_text_tuple(ff_raw.get("demuxers"), "ffmpeg.demuxers"),
        parsers=_text_tuple(ff_raw.get("parsers"), "ffmpeg.parsers"),
        bsfs=_text_tuple(ff_raw.get("bsfs"), "ffmpeg.bsfs"),
        protocols=_text_tuple(ff_raw.get("protocols"), "ffmpeg.protocols"),
        filters=_text_tuple(ff_raw.get("filters"), "ffmpeg.filters"),
    )

    ver_raw = _object(raw.get("verify"), "Verify configuration")
    glibc_raw = ver_raw.get("maxGlibcVersion")
    if (
        not isinstance(glibc_raw, list)
        or len(glibc_raw) != 2
        or any(type(part) is not int or part < 0 for part in glibc_raw)
    ):
        raise ValueError("verify.maxGlibcVersion must be a 2-integer version list")
    if tuple(glibc_raw) > (2, 35):
        raise ValueError("verify.maxGlibcVersion must not exceed the glibc 2.35 baseline")
    required_exports = frozenset(
        _text_tuple(ver_raw.get("requiredMpvExports"), "verify.requiredMpvExports")
    )
    if not MINIMUM_MPV_EXPORTS.issubset(required_exports):
        raise ValueError(
            "verify.requiredMpvExports is missing required runtime APIs: "
            f"{sorted(MINIMUM_MPV_EXPORTS - required_exports)}"
        )
    if any(not re.fullmatch(r"mpv_[A-Za-z0-9_]+", name) for name in required_exports):
        raise ValueError("verify.requiredMpvExports must contain mpv_* API names")
    verify_cfg = VerifyPolicyConfig(
        max_glibc_version=(glibc_raw[0], glibc_raw[1]),
        required_mpv_exports=required_exports,
        linux_system_libraries=frozenset(
            _text_tuple(
                ver_raw.get("linuxSystemLibraries"), "verify.linuxSystemLibraries"
            )
        ),
        android_system_libraries=frozenset(
            _text_tuple(
                ver_raw.get("androidSystemLibraries"), "verify.androidSystemLibraries"
            )
        ),
        windows_system_dlls=frozenset(
            _text_tuple(ver_raw.get("windowsSystemDlls"), "verify.windowsSystemDlls")
        ),
        windows_system_dll_prefixes=_text_tuple(
            ver_raw.get("windowsSystemDllPrefixes"), "verify.windowsSystemDllPrefixes"
        ),
    )

    upd_raw = _object(raw.get("update"), "Update configuration")
    gitlinks_raw = _object(upd_raw.get("requiredGitlinks"), "update.requiredGitlinks")
    required_gitlinks = {
        _text(parent, "update.requiredGitlinks parent"): frozenset(
            _text_tuple(paths, f"update.requiredGitlinks.{parent}")
        )
        for parent, paths in gitlinks_raw.items()
    }
    shaderc_deps_raw = upd_raw.get("shadercDeps")
    if not isinstance(shaderc_deps_raw, list) or not shaderc_deps_raw:
        raise ValueError("update.shadercDeps must be a non-empty list")
    shaderc_deps: list[tuple[str, str, str]] = []
    for item in shaderc_deps_raw:
        entry = _object(item, "update.shadercDeps entry")
        dep_name = _text(entry.get("name"), "update.shadercDeps.name")
        variable = _text(entry.get("variable"), "update.shadercDeps.variable")
        repository = _text(entry.get("repository"), "update.shadercDeps.repository")
        validate_https_url(repository, f"update.shadercDeps.{dep_name}")
        shaderc_deps.append((dep_name, variable, repository))
    update_cfg = UpdatePinsConfig(
        required_gitlinks=required_gitlinks,
        shaderc_deps=tuple(shaderc_deps),
    )

    return BuildConfig(
        toolchain=toolchain_cfg,
        optimization_tiers=optimization_tiers,
        static_packages=static_packages,
        recipes=recipes,
        patches=patches_cfg,
        ffmpeg=ffmpeg_cfg,
        verify=verify_cfg,
        update=update_cfg,
        raw=raw,
    )


BUILD_CONFIG = load_build_config()

OPTIMIZATION_TIERS = BUILD_CONFIG.optimization_tiers
STATIC_PACKAGES = BUILD_CONFIG.static_packages
FFMPEG_DECODERS = BUILD_CONFIG.ffmpeg.decoders_common
FFMPEG_ANDROID_DECODERS = BUILD_CONFIG.ffmpeg.decoders_android
FFMPEG_DESKTOP_DECODERS = BUILD_CONFIG.ffmpeg.decoders_desktop
FFMPEG_DEMUXERS = BUILD_CONFIG.ffmpeg.demuxers
FFMPEG_PARSERS = BUILD_CONFIG.ffmpeg.parsers
FFMPEG_BSFS = BUILD_CONFIG.ffmpeg.bsfs
FFMPEG_PROTOCOLS = BUILD_CONFIG.ffmpeg.protocols
FFMPEG_FILTERS = BUILD_CONFIG.ffmpeg.filters
REQUIRED_MPV_EXPORTS = BUILD_CONFIG.verify.required_mpv_exports
LINUX_SYSTEM_LIBRARIES = BUILD_CONFIG.verify.linux_system_libraries
MAX_GLIBC_VERSION = BUILD_CONFIG.verify.max_glibc_version
ANDROID_SYSTEM_LIBRARIES = BUILD_CONFIG.verify.android_system_libraries
WINDOWS_SYSTEM_DLLS = BUILD_CONFIG.verify.windows_system_dlls
WINDOWS_SYSTEM_DLL_PREFIXES = BUILD_CONFIG.verify.windows_system_dll_prefixes
