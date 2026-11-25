"""Toolchain resolution, verified caching, and compiler/environment setup."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ROOT
from .config import (
    BUILD_CONFIG,
    SHA256_RE,
    LockFile,
    TargetProfile,
    validate_https_url,
)
from .fs import (
    archive_hash_tree,
    atomic_replace_dir,
    capture_tree_manifest,
    extract_archive_safe,
    sha256_file,
    verify_tree_manifest,
)
from .locks import cache_lock
from .process import capture_cmd, require_executable

TOOLCHAIN_MARKER = ".complete.json"


@dataclass(frozen=True)
class ToolchainContext:
    profile: TargetProfile
    toolroot: Path | None
    prefix: Path
    cc: str
    cxx: str
    ar: str
    ranlib: str
    nm: str
    strip: str
    objcopy: str
    windres: str | None
    compiler_version: str
    cflags: list[str]
    cxxflags: list[str]
    ldflags: list[str]
    env: dict[str, str]
    llvm_version: str | None = None

    def cflags_for(
        self, opt: str = "Oz", *, hidden_visibility: bool = True
    ) -> list[str]:
        flags = [
            flag
            for flag in self.cflags
            if not flag.startswith("-O")
            and (hidden_visibility or flag != "-fvisibility=hidden")
        ]
        return [f"-{opt}", *flags]

    def cxxflags_for(
        self, opt: str = "Oz", *, hidden_visibility: bool = True
    ) -> list[str]:
        flags = [
            flag
            for flag in self.cxxflags
            if not flag.startswith("-O")
            and (
                hidden_visibility
                or flag not in ("-fvisibility=hidden", "-fvisibility-inlines-hidden")
            )
        ]
        return [f"-{opt}", *flags]

    def env_for(
        self, opt: str = "Oz", *, hidden_visibility: bool = True
    ) -> dict[str, str]:
        env = self.env.copy()
        env["CFLAGS"] = shlex.join(
            self.cflags_for(opt, hidden_visibility=hidden_visibility)
        )
        env["CXXFLAGS"] = shlex.join(
            self.cxxflags_for(opt, hidden_visibility=hidden_visibility)
        )
        return env


def restore_toolchain_archive(
    record: dict, cache_root: Path, *, verify_cache: bool = False
) -> Path:
    """Download, verify, and atomically extract a locked toolchain archive."""
    digest = record.get("sha256", "")
    url = record.get("url", "")
    if not SHA256_RE.fullmatch(digest):
        raise RuntimeError("Toolchain SHA-256 must be present in dependencies.json")
    validate_https_url(url, "Toolchain URL")
    with cache_lock(cache_root, "toolchain", digest):
        return _restore_toolchain_unlocked(
            url, digest, cache_root, verify_cache=verify_cache
        )


def _restore_toolchain_unlocked(
    url: str, digest: str, cache_root: Path, *, verify_cache: bool
) -> Path:
    destination = cache_root / "toolchains" / digest
    marker = destination / TOOLCHAIN_MARKER
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / (
        digest + (".zip" if url.endswith(".zip") else ".tar.xz")
    )

    if not verify_cache and marker.is_file() and not marker.is_symlink():
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            if (
                isinstance(recorded, dict)
                and recorded.get("schemaVersion") == 2
                and recorded.get("sha256") == digest
                and isinstance(recorded.get("manifest"), dict)
                and verify_tree_manifest(
                    destination, recorded["manifest"], exclude=(TOOLCHAIN_MARKER,)
                )
            ):
                return destination
        except (OSError, ValueError):
            pass

    if not archive.is_file() or archive.is_symlink() or sha256_file(archive) != digest:
        with tempfile.NamedTemporaryFile(
            dir=archive.parent, prefix=f".{digest}-", delete=False
        ) as target:
            temporary = Path(target.name)
            try:
                with urllib.request.urlopen(url, timeout=120) as response:
                    shutil.copyfileobj(response, target)
                target.flush()
                if sha256_file(temporary) != digest:
                    raise RuntimeError(f"Toolchain SHA-256 mismatch: {url}")
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)

    if verify_cache and marker.is_file() and not marker.is_symlink():
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            if (
                isinstance(recorded, dict)
                and recorded.get("schemaVersion") == 2
                and recorded.get("sha256") == digest
                and isinstance(recorded.get("manifest"), dict)
            ):
                strict = archive_hash_tree(archive)
                if verify_tree_manifest(
                    destination,
                    recorded["manifest"],
                    exclude=(TOOLCHAIN_MARKER,),
                    strict_hashes=strict,
                ):
                    return destination
        except (OSError, ValueError):
            pass

    staging = Path(tempfile.mkdtemp(prefix=f".{digest}-", dir=destination.parent))
    try:
        extract_archive_safe(archive, staging)
        manifest = capture_tree_manifest(staging, exclude=(TOOLCHAIN_MARKER,))
        if verify_cache:
            strict = archive_hash_tree(archive)
            if not verify_tree_manifest(
                staging, manifest, exclude=(TOOLCHAIN_MARKER,), strict_hashes=strict
            ):
                raise RuntimeError(
                    "Extracted toolchain differs from its verified immutable archive"
                )
        payload = {"schemaVersion": 2, "sha256": digest, "manifest": manifest}
        (staging / TOOLCHAIN_MARKER).write_text(
            json.dumps(payload, indent=4, sort_keys=True) + "\n", encoding="utf-8"
        )
        atomic_replace_dir(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def find_toolchain_root(root: Path, marker: str) -> Path:
    if (root / marker).is_file():
        return root
    choices = [
        item for item in root.iterdir() if item.is_dir() and (item / marker).is_file()
    ]
    if len(choices) != 1:
        raise RuntimeError(f"Expected one toolchain containing {marker} under {root}")
    return choices[0]


def resolve_android_ndk(
    lock: LockFile,
    ndk_arg: str | None,
    cache_root: Path,
    *,
    verify_cache: bool = False,
) -> Path:
    """Resolve Android NDK via explicit flag, standard SDK env vars, or locked archive download."""
    record = lock.toolchains["android"].raw
    revision = record["revision"]
    if ndk_arg:
        explicit = Path(ndk_arg).expanduser()
        if not (explicit / "source.properties").is_file():
            raise RuntimeError(
                f"The requested Android NDK directory is invalid: {ndk_arg}"
            )
        root = explicit.resolve()
    else:
        candidates: list[str | None] = [
            os.environ.get("ANDROID_NDK_ROOT"),
            os.environ.get("ANDROID_NDK_HOME"),
        ]
        for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            sdk = os.environ.get(variable)
            if sdk:
                candidates.append(str(Path(sdk).expanduser() / "ndk" / revision))
        root = next(
            (
                Path(value).expanduser().resolve()
                for value in candidates
                if value and (Path(value).expanduser() / "source.properties").is_file()
            ),
            None,
        )
        if root is None:
            root = find_toolchain_root(
                restore_toolchain_archive(
                    record, cache_root, verify_cache=verify_cache
                ),
                "source.properties",
            )
    properties = (root / "source.properties").read_text(encoding="utf-8")
    actual = re.search(r"^Pkg\.Revision\s*=\s*(\S+)\s*$", properties, re.MULTILINE)
    if not actual or actual.group(1) != revision:
        raise RuntimeError(f"Android NDK must be {revision}, found {root}")
    return root


def _strip_host_compiler_env(env: dict[str, str]) -> None:
    for key in (
        "CC",
        "CXX",
        "CFLAGS",
        "CXXFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "CPATH",
        "LIBRARY_PATH",
        "C_INCLUDE_PATH",
        "CPLUS_INCLUDE_PATH",
        "PKG_CONFIG_PATH",
        "PKG_CONFIG_SYSROOT_DIR",
    ):
        env.pop(key, None)


def _max_source_date_epoch(lock: LockFile) -> str | None:
    dates = [spec.date for spec in lock.dependencies.values() if spec.date]
    if not dates:
        return None
    instants = []
    for value in dates:
        source_date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if source_date.tzinfo is None:
            source_date = source_date.replace(tzinfo=timezone.utc)
        instants.append(source_date.timestamp())
    return str(int(max(instants)))


def prepare_toolchain(
    lock: LockFile,
    profile: TargetProfile,
    prefix: Path,
    *,
    ndk: str | None = None,
    toolchain: str | None = None,
    cache_root: Path | None = None,
    verify_cache: bool = False,
) -> ToolchainContext:
    cache = cache_root or (ROOT / ".cache")
    env = os.environ.copy()
    _strip_host_compiler_env(env)
    toolroot: Path | None = None
    windres: str | None = None
    llvm_version: str | None = None

    if profile.system == "android":
        toolroot = resolve_android_ndk(lock, ndk, cache, verify_cache=verify_cache)
        binaries = toolroot / "toolchains/llvm/prebuilt/linux-x86_64/bin"
        cc = binaries / f"{profile.triple}{profile.api}-clang"
        cxx = binaries / f"{profile.triple}{profile.api}-clang++"
    elif profile.system == "windows":
        record = lock.toolchains["windows"].raw
        if toolchain:
            toolroot = Path(toolchain).expanduser().resolve()
        else:
            toolroot = find_toolchain_root(
                restore_toolchain_archive(record, cache, verify_cache=verify_cache),
                "bin/x86_64-w64-mingw32-clang",
            )
        binaries = toolroot / "bin"
        cc = binaries / "x86_64-w64-mingw32-clang"
        cxx = binaries / "x86_64-w64-mingw32-clang++"
        windres_path = binaries / "x86_64-w64-mingw32-windres"
        windres = str(windres_path)
    else:
        release_info = Path("/etc/os-release").read_text(encoding="utf-8")
        ldd_line = capture_cmd(["ldd", "--version"]).splitlines()[0]
        if (
            os.environ.get("LIBMPV_BUILD_CONTAINER") != "1"
            or 'VERSION_ID="22.04"' not in release_info
            or "2.35" not in ldd_line
        ):
            raise RuntimeError(
                "Linux builds require the pinned Ubuntu 22.04 container; run with --container"
            )
        llvm_version = str(lock.toolchains["linux"].raw["llvm"])
        cc = Path(require_executable(f"clang-{llvm_version}"))
        cxx = Path(require_executable(f"clang++-{llvm_version}"))
        binaries = cc.parent

    for tool in (cc, cxx):
        if not tool.is_file():
            raise RuntimeError(f"Compiler is missing: {tool}")

    env["PATH"] = (
        str(prefix / "bin")
        + os.pathsep
        + str(binaries)
        + os.pathsep
        + env.get("PATH", "")
    )
    if profile.system == "linux":
        assert llvm_version is not None
        ar = require_executable(f"llvm-ar-{llvm_version}")
        ranlib = require_executable(f"llvm-ranlib-{llvm_version}")
        nm = require_executable(f"llvm-nm-{llvm_version}")
        strip = require_executable(f"llvm-strip-{llvm_version}")
        objcopy = require_executable(f"llvm-objcopy-{llvm_version}")
    else:
        tools: dict[str, str] = {}
        for attr, name in (
            ("ar", "llvm-ar"),
            ("ranlib", "llvm-ranlib"),
            ("nm", "llvm-nm"),
            ("strip", "llvm-strip"),
            ("objcopy", "llvm-objcopy"),
        ):
            candidate = binaries / name
            if profile.system == "windows" and not candidate.is_file():
                candidate = (
                    binaries / f"x86_64-w64-mingw32-{name.removeprefix('llvm-')}"
                )
            tools[attr] = str(candidate)
        ar = tools["ar"]
        ranlib = tools["ranlib"]
        nm = tools["nm"]
        strip = tools["strip"]
        objcopy = tools["objcopy"]

    tc_cfg = BUILD_CONFIG.toolchain
    march_flag = f"-march={tc_cfg.march[profile.cpu]}"
    sys_cfg = tc_cfg.systems[profile.system]
    cflags = [
        f"-{tc_cfg.default_opt}",
        march_flag,
        *tc_cfg.common_cflags,
        f"-flto={tc_cfg.lto}",
        f"-ffile-prefix-map={ROOT}=.",
        f"-I{prefix}/include",
        *sys_cfg.cflags,
    ]
    linker = f"-fuse-ld=lld-{llvm_version}" if profile.system == "linux" else "-fuse-ld=lld"
    ldflags = [linker, *tc_cfg.common_ldflags, *sys_cfg.ldflags]
    cxxflags = [*cflags, *sys_cfg.cxxflags]

    env.update(
        CC=str(cc),
        CXX=str(cxx),
        AR=ar,
        RANLIB=ranlib,
        NM=nm,
        STRIP=strip,
        CFLAGS=shlex.join(cflags),
        CXXFLAGS=shlex.join(cxxflags),
        CPPFLAGS=f"-I{prefix}/include",
        LDFLAGS=shlex.join(ldflags + [f"-L{prefix}/lib"]),
        CMAKE_PREFIX_PATH=str(prefix),
    )

    pc_paths = [str(prefix / "lib/pkgconfig"), str(prefix / "share/pkgconfig")]
    if profile.system == "linux":
        pc_paths += ["/usr/lib/x86_64-linux-gnu/pkgconfig", "/usr/share/pkgconfig"]
    env["PKG_CONFIG_LIBDIR"] = os.pathsep.join(pc_paths)

    epoch = _max_source_date_epoch(lock)
    if epoch is not None:
        env["SOURCE_DATE_EPOCH"] = epoch

    compiler_version = capture_cmd([str(cc), "--version"])
    return ToolchainContext(
        profile=profile,
        toolroot=toolroot,
        prefix=prefix,
        cc=str(cc),
        cxx=str(cxx),
        ar=ar,
        ranlib=ranlib,
        nm=nm,
        strip=strip,
        objcopy=objcopy,
        windres=windres,
        compiler_version=compiler_version,
        cflags=cflags,
        cxxflags=cxxflags,
        ldflags=ldflags,
        env=env,
        llvm_version=llvm_version,
    )
