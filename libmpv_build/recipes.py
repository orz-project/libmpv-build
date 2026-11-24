"""Declarative dependency DAG, per-dependency incremental identities, and build recipes."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import ROOT
from .config import (
    FFMPEG_FILTERS,
    LINUX_SYSTEM_LIBRARIES,
    STATIC_PACKAGES,
    LockFile,
    TargetProfile,
)
from .patching import patch_mpv_meson, rewrite_linux_pc_system_libs
from .process import run_cmd
from .runners import (
    extract_staged_prefix,
    run_autotools,
    run_cmake,
    run_meson,
    write_pkgconfig,
)
from .toolchains import ToolchainContext


@dataclass
class BuildContext:
    lock: LockFile
    tc: ToolchainContext
    base_dir: Path
    jobs: int
    prepare_source: Callable[[str], Path]
    build_dir_for: Callable[[str], Path]


@dataclass(frozen=True)
class Recipe:
    name: str
    deps_for: Callable[[str], tuple[str, ...]]
    signature_for: Callable[[TargetProfile, LockFile], dict]
    build: Callable[[BuildContext, Path, Path], None]
    extra_inputs_for: Callable[[str], tuple[str, ...]] = lambda _sys: ()
    helpers: tuple[Callable, ...] = ()


def _deps_none(_sys: str) -> tuple[str, ...]:
    return ()


def _build_zlib(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    source = ctx.prepare_source("zlib")
    run_cmd(
        ["sh", source / "configure", "--static", f"--prefix={ctx.tc.prefix}"],
        cwd=source,
        env=ctx.tc.env,
    )
    run_cmd(["make", f"-j{ctx.jobs}", "libz.a"], cwd=source, env=ctx.tc.env)
    run_cmd(["make", f"DESTDIR={destdir}", "install"], cwd=source, env=ctx.tc.env)
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)
    write_pkgconfig(stage_prefix, "zlib", "z", ctx.lock.dependencies["zlib"].version)


def _build_dav1d(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("dav1d"),
        ctx.build_dir_for("dav1d"),
        destdir,
        ctx.jobs,
        ["enable_tests=false", "enable_tools=false", "stack_alignment=16"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_freetype(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("freetype"),
        ctx.build_dir_for("freetype"),
        destdir,
        ctx.jobs,
        [
            "zlib=enabled",
            "bzip2=disabled",
            "png=disabled",
            "harfbuzz=disabled",
            "brotli=disabled",
        ],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_fribidi(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("fribidi"),
        ctx.build_dir_for("fribidi"),
        destdir,
        ctx.jobs,
        ["tests=false", "docs=false"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_harfbuzz(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("harfbuzz"),
        ctx.build_dir_for("harfbuzz"),
        destdir,
        ctx.jobs,
        [
            "auto_features=disabled",
            "tests=disabled",
            "docs=disabled",
            "utilities=disabled",
            "glib=disabled",
            "gobject=disabled",
            "icu=disabled",
            "introspection=disabled",
            "freetype=enabled",
            "raster=disabled",
            "vector=disabled",
            "gpu=disabled",
            "subset=disabled",
        ],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_libunibreak(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_autotools(
        ctx.tc,
        ctx.prepare_source("libunibreak"),
        ctx.build_dir_for("libunibreak"),
        destdir,
        ctx.jobs,
        [],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_libxml2(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("libxml2"),
        ctx.build_dir_for("libxml2"),
        destdir,
        ctx.jobs,
        [
            "minimum=true",
            "push=enabled",
            "reader=enabled",
            "sax1=enabled",
            "iso8859x=enabled",
            "pattern=enabled",
        ],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _fontconfig_options(system: str) -> list[str]:
    options = [
        "tests=disabled",
        "doc=disabled",
        "tools=disabled",
        "nls=disabled",
        "cache-build=disabled",
        "tests-external-fonts=disabled",
        "xml-backend=libxml2",
    ]
    if system == "linux":
        options += [
            "baseconfig-dir=/etc/fonts",
            "config-dir=/etc/fonts/conf.d",
            "template-dir=/usr/share/fontconfig/conf.avail",
            "cache-dir=/var/cache/fontconfig",
        ]
    elif system == "android":
        options += [
            "default-fonts-dirs=/system/fonts",
            "additional-fonts-dirs=/system/fonts",
            "baseconfig-dir=/system/etc/fonts",
            "config-dir=/system/etc/fonts/conf.d",
            "template-dir=/system/etc/fonts/conf.avail",
            "cache-dir=/nonexistent/fontconfig",
        ]
    return options


def _build_fontconfig(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("fontconfig"),
        ctx.build_dir_for("fontconfig"),
        destdir,
        ctx.jobs,
        _fontconfig_options(ctx.tc.profile.system),
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_libass(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_autotools(
        ctx.tc,
        ctx.prepare_source("libass"),
        ctx.build_dir_for("libass"),
        destdir,
        ctx.jobs,
        list(ctx.tc.profile.libass),
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_vulkan_headers(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_cmake(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("vulkan-headers"),
        ctx.build_dir_for("vulkan-headers"),
        destdir,
        ctx.jobs,
        ["-DVULKAN_HEADERS_ENABLE_TESTS=OFF", "-DVULKAN_HEADERS_ENABLE_INSTALL=ON"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_shaderc(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    source = ctx.prepare_source("shaderc")
    for name, relative in (
        ("glslang", "glslang"),
        ("spirv-tools", "spirv-tools"),
        ("spirv-headers", "spirv-tools/external/spirv-headers"),
    ):
        destination = source / "third_party" / relative
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(ctx.prepare_source(name), destination, symlinks=True)
    build_dir = run_cmake(
        ctx.tc,
        ctx.base_dir,
        source,
        ctx.build_dir_for("shaderc"),
        destdir,
        ctx.jobs,
        [
            "-DSHADERC_SKIP_TESTS=ON",
            "-DSHADERC_SKIP_EXAMPLES=ON",
            "-DSHADERC_SKIP_EXECUTABLES=ON",
            "-DSHADERC_SKIP_INSTALL=OFF",
            "-DSHADERC_ENABLE_WERROR_COMPILE=OFF",
            "-DSPIRV_SKIP_TESTS=ON",
            "-DSPIRV_SKIP_EXECUTABLES=ON",
            "-DENABLE_GLSLANG_BINARIES=OFF",
            "-DENABLE_SPVREMAPPER=OFF",
            "-DSPIRV_TOOLS_BUILD_STATIC=ON",
        ],
        targets=["shaderc_combined"],
        install=False,
    )
    (stage_prefix / "lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        build_dir / "libshaderc/libshaderc_combined.a",
        stage_prefix / "lib/libshaderc_combined.a",
    )
    shutil.copytree(
        source / "libshaderc/include/shaderc",
        stage_prefix / "include/shaderc",
        dirs_exist_ok=True,
    )
    write_pkgconfig(
        stage_prefix,
        "shaderc",
        "shaderc_combined",
        ctx.lock.dependencies["shaderc"].version,
    )


def _build_spirv_cross(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    source = ctx.prepare_source("spirv-cross")
    run_cmake(
        ctx.tc,
        ctx.base_dir,
        source,
        ctx.build_dir_for("spirv-cross"),
        destdir,
        ctx.jobs,
        [
            "-DSPIRV_CROSS_STATIC=ON",
            "-DSPIRV_CROSS_SHARED=OFF",
            "-DSPIRV_CROSS_CLI=OFF",
            "-DSPIRV_CROSS_ENABLE_TESTS=OFF",
            "-DSPIRV_CROSS_ENABLE_C_API=ON",
            "-DSPIRV_CROSS_ENABLE_MSL=OFF",
            "-DSPIRV_CROSS_ENABLE_CPP=OFF",
            "-DSPIRV_CROSS_ENABLE_REFLECT=OFF",
            "-DSPIRV_CROSS_ENABLE_UTIL=OFF",
        ],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)
    header = (source / "spirv_cross_c.h").read_text(encoding="utf-8")
    fields = [
        re.search(rf"#define\s+SPVC_C_API_VERSION_{part}\s+(\d+)", header)
        for part in ("MAJOR", "MINOR", "PATCH")
    ]
    if not all(fields):
        raise RuntimeError("Cannot derive the pinned SPIRV-Cross C API version")
    version = ".".join(field.group(1) for field in fields if field is not None)
    write_pkgconfig(
        stage_prefix,
        "spirv-cross-c-shared",
        "spirv-cross-c",
        version,
        private="-lspirv-cross-hlsl -lspirv-cross-glsl -lspirv-cross-core",
        extra_cflags="-I${includedir}/spirv_cross",
    )


def _build_libplacebo(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    options = [
        "auto_features=disabled",
        "opengl=enabled",
        "dovi=enabled",
        "demos=false",
        "tests=false",
        "bench=false",
    ]
    if ctx.tc.profile.system == "windows":
        options += ["d3d11=enabled", "shaderc=enabled"]
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("libplacebo"),
        ctx.build_dir_for("libplacebo"),
        destdir,
        ctx.jobs,
        options,
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)
    pc = stage_prefix / "lib/pkgconfig/libplacebo.pc"
    contents = pc.read_text(encoding="utf-8")
    contents = re.sub(r"(?<!\S)-l(?:stdc\+\+|c\+\+|c\+\+_shared)(?!\S)", "", contents)
    pc.write_text(contents, encoding="utf-8")


def _ffmpeg_flags(tc: ToolchainContext) -> tuple[list[str], dict[str, str]]:
    env = tc.env.copy()
    ff_cflags = ["-O2" if flag == "-Os" else flag for flag in tc.cflags]
    env["CFLAGS"] = shlex.join(ff_cflags)
    flags = [
        f"--prefix={tc.prefix}",
        "--enable-static",
        "--disable-shared",
        "--enable-pic",
        "--enable-lto=thin",
        "--optflags=-O2",
        "--disable-autodetect",
        "--disable-programs",
        "--disable-doc",
        "--disable-debug",
        "--disable-stripping",
        "--disable-devices",
        "--disable-avdevice",
        "--disable-encoders",
        "--disable-muxers",
        "--disable-protocols",
        "--enable-protocol=file,http,tcp,crypto",
        "--disable-filters",
        f"--enable-filter={','.join(FFMPEG_FILTERS)}",
        "--disable-vulkan",
        "--enable-gpl",
        "--enable-version3",
        "--enable-zlib",
        "--disable-bzlib",
        "--disable-lzma",
        "--enable-libdav1d",
        f"--cc={tc.cc}",
        f"--cxx={tc.cxx}",
        f"--ar={tc.ar}",
        f"--ranlib={tc.ranlib}",
        f"--nm={tc.nm}",
        "--pkg-config=pkg-config",
        "--pkg-config-flags=--static",
        f"--arch={tc.profile.cpu}",
        f"--extra-cflags={env['CFLAGS']} -I{tc.prefix}/include",
        f"--extra-ldflags={env['LDFLAGS']}",
        *tc.profile.ffmpeg,
    ]
    if tc.profile.system in ("android", "windows"):
        flags += [
            "--enable-cross-compile",
            f"--cross-prefix={tc.profile.triple}-",
            f"--target-os={'android' if tc.profile.system == 'android' else 'mingw32'}",
        ]
    if tc.profile.cpu == "aarch64":
        flags += ["--cpu=armv8-a"]
    return flags, env


def _build_ffmpeg(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    source = ctx.prepare_source("ffmpeg")
    build_dir = ctx.build_dir_for("ffmpeg")
    build_dir.mkdir(parents=True, exist_ok=True)
    flags, env = _ffmpeg_flags(ctx.tc)
    run_cmd([source / "configure", *flags], cwd=build_dir, env=env)
    run_cmd(["make", f"-j{ctx.jobs}"], cwd=build_dir, env=env)
    run_cmd(["make", f"DESTDIR={destdir}", "install"], cwd=build_dir, env=env)
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _mpv_final_link_args(tc: ToolchainContext) -> list[str]:
    final = ["-Wl,--as-needed", f"-L{tc.prefix}/lib"]
    if tc.profile.system == "android":
        assert tc.toolroot is not None
        runtime = (
            tc.toolroot
            / "toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib"
            / tc.profile.triple
        )
        final += [
            "-Wl,--undefined=av_jni_set_java_vm",
            f"-Wl,--version-script={ROOT / 'patches/android-exports.map'}",
            str(runtime / "libc++_static.a"),
            str(runtime / "libc++abi.a"),
        ]
    elif tc.profile.system == "linux":
        final += [
            f"-Wl,--version-script={ROOT / 'patches/desktop-exports.map'}",
            "-Wl,-Bstatic",
            "-lstdc++",
            "-Wl,-Bdynamic",
            "-static-libgcc",
        ]
    else:
        final += ["-Wl,--exclude-all-symbols", "-static-libstdc++"]
    return final


def _build_mpv(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    if ctx.tc.profile.system == "linux":
        rewrite_linux_pc_system_libs(
            ctx.tc.prefix / "lib/pkgconfig", LINUX_SYSTEM_LIBRARIES
        )
    source = ctx.prepare_source("mpv")
    patch_mpv_meson(
        source,
        final_link_args=_mpv_final_link_args(ctx.tc),
        enable_cpp_link=(ctx.tc.profile.system == "windows"),
    )
    common = [
        "auto_features=disabled",
        "libmpv=true",
        "cplayer=false",
        "tests=false",
        "fuzzers=false",
        "build-date=false",
        "lua=disabled",
        "gl=enabled",
        "plain-gl=enabled",
        "zlib=enabled",
        "vector=enabled",
    ]
    run_meson(
        ctx.tc,
        ctx.base_dir,
        source,
        ctx.build_dir_for("mpv"),
        destdir,
        ctx.jobs,
        common + list(ctx.tc.profile.mpv),
        shared=True,
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


RECIPES: dict[str, Recipe] = {
    "zlib": Recipe(
        name="zlib",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {"recipe": "zlib-static-v1"},
        build=_build_zlib,
    ),
    "dav1d": Recipe(
        name="dav1d",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": [
                "enable_tests=false",
                "enable_tools=false",
                "stack_alignment=16",
            ]
        },
        build=_build_dav1d,
    ),
    "freetype": Recipe(
        name="freetype",
        deps_for=lambda _s: ("zlib",),
        signature_for=lambda _p, _l: {
            "options": [
                "zlib=enabled",
                "bzip2=disabled",
                "png=disabled",
                "harfbuzz=disabled",
                "brotli=disabled",
            ]
        },
        build=_build_freetype,
    ),
    "fribidi": Recipe(
        name="fribidi",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {"options": ["tests=false", "docs=false"]},
        build=_build_fribidi,
    ),
    "harfbuzz": Recipe(
        name="harfbuzz",
        deps_for=lambda _s: ("freetype",),
        signature_for=lambda _p, _l: {
            "options": ["freetype=enabled", "auto_features=disabled"]
        },
        build=_build_harfbuzz,
    ),
    "libunibreak": Recipe(
        name="libunibreak",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {"recipe": "libunibreak-autotools-v1"},
        build=_build_libunibreak,
    ),
    "libxml2": Recipe(
        name="libxml2",
        deps_for=lambda _s: ("zlib",),
        signature_for=lambda _p, _l: {
            "options": [
                "minimum=true",
                "push=enabled",
                "reader=enabled",
                "sax1=enabled",
                "iso8859x=enabled",
                "pattern=enabled",
            ]
        },
        build=_build_libxml2,
    ),
    "fontconfig": Recipe(
        name="fontconfig",
        deps_for=lambda _s: ("freetype", "libxml2"),
        signature_for=lambda p, _l: {"options": _fontconfig_options(p.system)},
        build=_build_fontconfig,
        helpers=(_fontconfig_options,),
    ),
    "libass": Recipe(
        name="libass",
        deps_for=lambda s: (
            ("freetype", "fribidi", "harfbuzz", "libunibreak")
            if s == "windows"
            else ("freetype", "fribidi", "harfbuzz", "libunibreak", "fontconfig")
        ),
        signature_for=lambda p, _l: {"options": list(p.libass)},
        build=_build_libass,
    ),
    "vulkan-headers": Recipe(
        name="vulkan-headers",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": [
                "-DVULKAN_HEADERS_ENABLE_TESTS=OFF",
                "-DVULKAN_HEADERS_ENABLE_INSTALL=ON",
            ]
        },
        build=_build_vulkan_headers,
    ),
    "shaderc": Recipe(
        name="shaderc",
        deps_for=_deps_none,
        extra_inputs_for=lambda _s: ("glslang", "spirv-tools", "spirv-headers"),
        signature_for=lambda _p, _l: {"recipe": "shaderc-combined-v1"},
        build=_build_shaderc,
    ),
    "spirv-cross": Recipe(
        name="spirv-cross",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {"recipe": "spirv-cross-c-v1"},
        build=_build_spirv_cross,
    ),
    "libplacebo": Recipe(
        name="libplacebo",
        deps_for=lambda s: (
            ("vulkan-headers", "shaderc", "spirv-cross")
            if s == "windows"
            else ("vulkan-headers",)
        ),
        signature_for=lambda p, _l: {
            "system": p.system,
            "d3d11": p.system == "windows",
            "shaderc": p.system == "windows",
        },
        build=_build_libplacebo,
    ),
    "ffmpeg": Recipe(
        name="ffmpeg",
        deps_for=lambda _s: ("zlib", "dav1d"),
        signature_for=lambda p, _l: {
            "opt": "O2",
            "filters": list(FFMPEG_FILTERS),
            "ffmpeg": list(p.ffmpeg),
        },
        build=_build_ffmpeg,
        helpers=(_ffmpeg_flags,),
    ),
    "mpv": Recipe(
        name="mpv",
        deps_for=lambda s: (
            ("zlib", "libass", "libplacebo", "ffmpeg")
            + (("shaderc", "spirv-cross") if s == "windows" else ())
        ),
        signature_for=lambda p, _l: {
            "mpv": list(p.mpv),
            "system": p.system,
            "library": p.library,
        },
        build=_build_mpv,
        helpers=(_mpv_final_link_args,),
    ),
}


def resolve_build_order(
    target: str, lock: LockFile, profile: TargetProfile
) -> list[str]:
    """Topologically sort all dependencies active for target."""
    active = {
        name
        for name, spec in lock.dependencies.items()
        if target in spec.targets and name in RECIPES
    }
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"Cyclic recipe dependency: {name}")
        if name not in active:
            raise ValueError(f"Dependency {name} is not active for target {target}")
        visiting.add(name)
        for dep in RECIPES[name].deps_for(profile.system):
            visit(dep)
        visiting.remove(name)
        visited.add(name)
        order.append(name)

    for candidate in sorted(active):
        visit(candidate)
    if "mpv" not in order:
        raise ValueError(f"Target {target} has no mpv recipe")
    return order


def _gitlink_records(
    lock: LockFile, name: str, seen: set[str] | None = None
) -> dict[str, dict]:
    visited = seen if seen is not None else set()
    if name in visited:
        return {}
    visited.add(name)
    records = {name: lock.dependencies[name].raw}
    for child, spec in lock.dependencies.items():
        pin = spec.pin_from
        if pin and pin.get("type") == "gitlink" and pin.get("parent") == name:
            records.update(_gitlink_records(lock, child, visited))
    return records


def dependency_identity(
    name: str,
    *,
    lock: LockFile,
    profile: TargetProfile,
    compiler_version: str,
    upstream_ids: dict[str, str],
    root: Path = ROOT,
) -> str:
    """Compute a narrow, per-dependency cache identity bound to upstream DAG outputs."""
    recipe = RECIPES[name]
    deps = recipe.deps_for(profile.system)
    lock_inputs = _gitlink_records(lock, name)
    for extra in recipe.extra_inputs_for(profile.system):
        lock_inputs.update(_gitlink_records(lock, extra))

    payload = {
        "cacheSchemaVersion": 2,
        "buildTools": lock.build_tools,
        "toolchain": lock.toolchains[profile.system].raw,
        "compiler": compiler_version,
        "prefix": str(root / "build" / profile.name / "prefix"),
        "target": {
            "name": profile.name,
            "system": profile.system,
            "cpu": profile.cpu,
            "triple": profile.triple,
            "api": profile.api,
        },
        "recipe": recipe.signature_for(profile, lock),
        "implementation": [
            inspect.getsource(function) for function in (recipe.build, *recipe.helpers)
        ],
        "staticPackages": STATIC_PACKAGES,
        "lockInputs": lock_inputs,
        "upstream": {dep: upstream_ids[dep] for dep in deps},
    }
    hasher = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
    for filename in (
        "builder.py",
        "runners.py",
        "toolchains.py",
        "patching.py",
        "process.py",
        "fs.py",
        "sources.py",
        "config.py",
        "../Dockerfile",
    ):
        hasher.update((root / "libmpv_build" / filename).read_bytes())
    if name == "mpv":
        map_file = root / (
            "patches/android-exports.map"
            if profile.system == "android"
            else "patches/desktop-exports.map"
        )
        if map_file.is_file():
            hasher.update(map_file.read_bytes())
    return hasher.hexdigest()
