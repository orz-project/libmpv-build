"""Declarative dependency DAG, per-dependency incremental identities, and build recipes."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import ROOT
from .config import (
    BUILD_CONFIG,
    FFMPEG_ANDROID_DECODERS,
    FFMPEG_BSFS,
    FFMPEG_DECODERS,
    FFMPEG_DEMUXERS,
    FFMPEG_DESKTOP_DECODERS,
    FFMPEG_FILTERS,
    FFMPEG_PARSERS,
    FFMPEG_PROTOCOLS,
    LINUX_SYSTEM_LIBRARIES,
    OPTIMIZATION_TIERS,
    STATIC_PACKAGES,
    LockFile,
    TargetProfile,
)
from .patching import (
    patch_freetype_modules,
    patch_mpv_meson,
    patch_mpv_wayland_compat,
    replace_exact,
    rewrite_linux_pc_system_libs,
)
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


def _recipe_list(name: str) -> list[str]:
    entry = BUILD_CONFIG.recipes[name]
    assert isinstance(entry, tuple)
    return list(entry)


def _recipe_section(name: str, section: str) -> list[str]:
    entry = BUILD_CONFIG.recipes[name]
    assert isinstance(entry, dict)
    return list(entry[section])


def _strip_cpp_stdlib_from_pc(pc_path: Path) -> None:
    contents = pc_path.read_text(encoding="utf-8")
    contents = re.sub(r"(?<!\S)-l(?:stdc\+\+|c\+\+|c\+\+_shared)(?!\S)", "", contents)
    pc_path.write_text(contents, encoding="utf-8")


def _build_zlib_ng(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_cmake(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("zlib-ng"),
        ctx.build_dir_for("zlib-ng"),
        destdir,
        ctx.jobs,
        _recipe_list("zlib-ng"),
        opt=OPTIMIZATION_TIERS["zlib-ng"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_dav1d(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("dav1d"),
        ctx.build_dir_for("dav1d"),
        destdir,
        ctx.jobs,
        _recipe_list("dav1d"),
        opt=OPTIMIZATION_TIERS["dav1d"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_freetype(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    source = ctx.prepare_source("freetype")
    patch_freetype_modules(source)
    run_meson(
        ctx.tc,
        ctx.base_dir,
        source,
        ctx.build_dir_for("freetype"),
        destdir,
        ctx.jobs,
        _recipe_list("freetype"),
        opt=OPTIMIZATION_TIERS["freetype"],
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
        _recipe_list("fribidi"),
        opt=OPTIMIZATION_TIERS["fribidi"],
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
        _recipe_list("harfbuzz"),
        opt=OPTIMIZATION_TIERS["harfbuzz"],
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
        opt=OPTIMIZATION_TIERS["libunibreak"],
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
        _recipe_list("libxml2"),
        opt=OPTIMIZATION_TIERS["libxml2"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _fontconfig_options(system: str) -> list[str]:
    options = _recipe_section("fontconfig", "common")
    if system in ("linux", "android"):
        options += _recipe_section("fontconfig", system)
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
        opt=OPTIMIZATION_TIERS["fontconfig"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_libass(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_autotools(
        ctx.tc,
        ctx.prepare_source("libass"),
        ctx.build_dir_for("libass"),
        destdir,
        ctx.jobs,
        [*ctx.tc.profile.libass, *_recipe_list("libass")],
        opt=OPTIMIZATION_TIERS["libass"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_uchardet(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    source = ctx.prepare_source("uchardet")
    replace_exact(source / "CMakeLists.txt", "add_subdirectory(test)\n", "")
    run_cmake(
        ctx.tc,
        ctx.base_dir,
        source,
        ctx.build_dir_for("uchardet"),
        destdir,
        ctx.jobs,
        _recipe_list("uchardet"),
        opt=OPTIMIZATION_TIERS["uchardet"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)
    _strip_cpp_stdlib_from_pc(stage_prefix / "lib/pkgconfig/uchardet.pc")


def _build_win_iconv(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_cmake(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("win-iconv"),
        ctx.build_dir_for("win-iconv"),
        destdir,
        ctx.jobs,
        _recipe_list("win-iconv"),
        opt=OPTIMIZATION_TIERS["win-iconv"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_libiconv(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_autotools(
        ctx.tc,
        ctx.prepare_source("libiconv"),
        ctx.build_dir_for("libiconv"),
        destdir,
        ctx.jobs,
        _recipe_list("libiconv"),
        opt=OPTIMIZATION_TIERS["libiconv"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_nv_codec_headers(
    ctx: BuildContext, destdir: Path, stage_prefix: Path
) -> None:
    source = ctx.prepare_source("nv-codec-headers")
    run_cmd(
        ["make", f"PREFIX={ctx.tc.prefix}", f"DESTDIR={destdir}", "install"],
        cwd=source,
        env=ctx.tc.env,
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_vulkan_headers(
    ctx: BuildContext, destdir: Path, stage_prefix: Path
) -> None:
    # libplacebo's disabled Vulkan stubs still use public Vulkan types
    run_cmake(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("vulkan-headers"),
        ctx.build_dir_for("vulkan-headers"),
        destdir,
        ctx.jobs,
        _recipe_list("vulkan-headers"),
        opt=OPTIMIZATION_TIERS["vulkan-headers"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


def _build_wayland(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("wayland"),
        ctx.build_dir_for("wayland"),
        destdir,
        ctx.jobs,
        _recipe_list("wayland"),
        shared=True,
        opt=OPTIMIZATION_TIERS["wayland"],
        native=True,
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)
    # Retain wayland-scanner, 1.23 headers, and .pc metadata; remove compiled
    # libwayland-* binaries so mpv always links Ubuntu 22.04's system 1.20 libs.
    libdir = stage_prefix / "lib"
    for pattern in ("libwayland-*.so*", "libwayland-*.a"):
        for artifact in libdir.glob(pattern):
            artifact.unlink()


def _build_wayland_protocols(
    ctx: BuildContext, destdir: Path, stage_prefix: Path
) -> None:
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("wayland-protocols"),
        ctx.build_dir_for("wayland-protocols"),
        destdir,
        ctx.jobs,
        _recipe_list("wayland-protocols"),
        opt=OPTIMIZATION_TIERS["wayland-protocols"],
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
        _recipe_list("shaderc"),
        targets=["shaderc_combined"],
        install=False,
        opt=OPTIMIZATION_TIERS["shaderc"],
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
        _recipe_list("spirv-cross"),
        opt=OPTIMIZATION_TIERS["spirv-cross"],
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
    options = _recipe_section("libplacebo", "common")
    if ctx.tc.profile.system == "windows":
        options += _recipe_section("libplacebo", "windows")
    run_meson(
        ctx.tc,
        ctx.base_dir,
        ctx.prepare_source("libplacebo"),
        ctx.build_dir_for("libplacebo"),
        destdir,
        ctx.jobs,
        options,
        opt=OPTIMIZATION_TIERS["libplacebo"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)
    _strip_cpp_stdlib_from_pc(stage_prefix / "lib/pkgconfig/libplacebo.pc")


def _ffmpeg_decoders(system: str) -> tuple[str, ...]:
    extra = FFMPEG_ANDROID_DECODERS if system == "android" else FFMPEG_DESKTOP_DECODERS
    return FFMPEG_DECODERS + extra


def _ffmpeg_flags(tc: ToolchainContext) -> tuple[list[str], dict[str, str]]:
    opt = OPTIMIZATION_TIERS["ffmpeg"]
    # Android FFmpeg exports av_jni_set_java_vm without an explicit visibility attribute;
    # Full LTO and android-exports.map still internalize all other symbols at link time.
    env = tc.env_for(opt, hidden_visibility=(tc.profile.system != "android"))
    decoders = _ffmpeg_decoders(tc.profile.system)
    flags = [
        f"--prefix={tc.prefix}",
        "--enable-static",
        "--disable-shared",
        "--enable-pic",
        f"--enable-lto={BUILD_CONFIG.toolchain.lto}",
        f"--optflags=-{opt}",
        "--disable-autodetect",
        "--disable-programs",
        "--disable-doc",
        "--disable-debug",
        "--disable-stripping",
        "--disable-devices",
        "--disable-avdevice",
        "--disable-encoders",
        "--disable-muxers",
        "--disable-decoders",
        f"--enable-decoder={','.join(decoders)}",
        "--disable-demuxers",
        f"--enable-demuxer={','.join(FFMPEG_DEMUXERS)}",
        "--disable-parsers",
        f"--enable-parser={','.join(FFMPEG_PARSERS)}",
        "--disable-bsfs",
        f"--enable-bsf={','.join(FFMPEG_BSFS)}",
        "--disable-protocols",
        f"--enable-protocol={','.join(FFMPEG_PROTOCOLS)}",
        "--disable-filters",
        f"--enable-filter={','.join(FFMPEG_FILTERS)}",
        "--disable-vulkan",
        "--enable-gpl",
        "--enable-version3",
        "--enable-zlib",
        "--enable-libxml2",
        "--enable-iconv",
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
            "--extra-libs=-liconv",
            "--enable-cross-compile",
            f"--cross-prefix={tc.profile.triple}-",
            f"--target-os={'android' if tc.profile.system == 'android' else 'mingw32'}",
        ]
    if tc.profile.cpu == "aarch64":
        flags += [f"--cpu={BUILD_CONFIG.toolchain.march['aarch64']}"]
    return flags, env


def _validate_ffmpeg_components(build_dir: Path, system: str) -> None:
    """Reject requested components silently disabled or misspelled by configure."""
    components = (build_dir / "config_components.h").read_text(encoding="utf-8")
    enabled = set(re.findall(r"^#define CONFIG_(\w+) 1$", components, re.MULTILINE))
    builtin_filters = {"buffer", "abuffer", "buffersink", "abuffersink"}
    requested = {
        "decoder": _ffmpeg_decoders(system),
        "demuxer": FFMPEG_DEMUXERS,
        "parser": FFMPEG_PARSERS,
        "bsf": FFMPEG_BSFS,
        "protocol": FFMPEG_PROTOCOLS,
        "filter": tuple(name for name in FFMPEG_FILTERS if name not in builtin_filters),
    }
    missing = [
        f"{name}_{kind}"
        for kind, names in requested.items()
        for name in names
        if f"{name}_{kind}".upper() not in enabled
    ]
    if missing:
        raise RuntimeError(
            "FFmpeg disabled requested components: " + ", ".join(missing)
        )


def _build_ffmpeg(ctx: BuildContext, destdir: Path, stage_prefix: Path) -> None:
    source = ctx.prepare_source("ffmpeg")
    build_dir = ctx.build_dir_for("ffmpeg")
    build_dir.mkdir(parents=True, exist_ok=True)
    flags, env = _ffmpeg_flags(ctx.tc)
    run_cmd([source / "configure", *flags], cwd=build_dir, env=env)
    _validate_ffmpeg_components(build_dir, ctx.tc.profile.system)
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
    if ctx.tc.profile.system == "linux":
        patch_mpv_wayland_compat(source)
    patch_mpv_meson(
        source,
        final_link_args=_mpv_final_link_args(ctx.tc),
        enable_cpp_link=(ctx.tc.profile.system == "windows"),
        static_iconv_dir=(
            ctx.tc.prefix / "lib" if ctx.tc.profile.system != "linux" else None
        ),
    )
    common = _recipe_list("mpv")
    run_meson(
        ctx.tc,
        ctx.base_dir,
        source,
        ctx.build_dir_for("mpv"),
        destdir,
        ctx.jobs,
        common + list(ctx.tc.profile.mpv),
        shared=True,
        opt=OPTIMIZATION_TIERS["mpv"],
    )
    extract_staged_prefix(destdir, ctx.tc.prefix, stage_prefix)


RECIPES: dict[str, Recipe] = {
    "zlib-ng": Recipe(
        name="zlib-ng",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("zlib-ng"),
            "opt": OPTIMIZATION_TIERS["zlib-ng"],
        },
        build=_build_zlib_ng,
    ),
    "dav1d": Recipe(
        name="dav1d",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("dav1d"),
            "opt": OPTIMIZATION_TIERS["dav1d"],
        },
        build=_build_dav1d,
    ),
    "freetype": Recipe(
        name="freetype",
        deps_for=lambda _s: ("zlib-ng",),
        signature_for=lambda _p, _l: {
            "options": _recipe_list("freetype"),
            "opt": OPTIMIZATION_TIERS["freetype"],
            "disabledModules": list(BUILD_CONFIG.patches.freetype_disabled_modules),
        },
        build=_build_freetype,
    ),
    "fribidi": Recipe(
        name="fribidi",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("fribidi"),
            "opt": OPTIMIZATION_TIERS["fribidi"],
        },
        build=_build_fribidi,
    ),
    "harfbuzz": Recipe(
        name="harfbuzz",
        deps_for=lambda _s: ("freetype",),
        signature_for=lambda _p, _l: {
            "options": _recipe_list("harfbuzz"),
            "opt": OPTIMIZATION_TIERS["harfbuzz"],
        },
        build=_build_harfbuzz,
    ),
    "libunibreak": Recipe(
        name="libunibreak",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "recipe": "libunibreak-autotools-v1",
            "opt": OPTIMIZATION_TIERS["libunibreak"],
        },
        build=_build_libunibreak,
    ),
    "libxml2": Recipe(
        name="libxml2",
        deps_for=lambda _s: ("zlib-ng",),
        signature_for=lambda _p, _l: {
            "options": _recipe_list("libxml2"),
            "opt": OPTIMIZATION_TIERS["libxml2"],
        },
        build=_build_libxml2,
    ),
    "fontconfig": Recipe(
        name="fontconfig",
        deps_for=lambda _s: ("freetype", "libxml2"),
        signature_for=lambda p, _l: {
            "options": _fontconfig_options(p.system),
            "opt": OPTIMIZATION_TIERS["fontconfig"],
        },
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
        signature_for=lambda p, _l: {
            "options": [*p.libass, *_recipe_list("libass")],
            "opt": OPTIMIZATION_TIERS["libass"],
        },
        build=_build_libass,
    ),
    "uchardet": Recipe(
        name="uchardet",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("uchardet"),
            "opt": OPTIMIZATION_TIERS["uchardet"],
        },
        build=_build_uchardet,
        helpers=(_strip_cpp_stdlib_from_pc,),
    ),
    "win-iconv": Recipe(
        name="win-iconv",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("win-iconv"),
            "opt": OPTIMIZATION_TIERS["win-iconv"],
        },
        build=_build_win_iconv,
    ),
    "libiconv": Recipe(
        name="libiconv",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("libiconv"),
            "opt": OPTIMIZATION_TIERS["libiconv"],
        },
        build=_build_libiconv,
    ),
    "nv-codec-headers": Recipe(
        name="nv-codec-headers",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {"recipe": "nv-codec-headers-v1"},
        build=_build_nv_codec_headers,
    ),
    "vulkan-headers": Recipe(
        name="vulkan-headers",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("vulkan-headers"),
        },
        build=_build_vulkan_headers,
    ),
    "wayland": Recipe(
        name="wayland",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("wayland"),
            "opt": OPTIMIZATION_TIERS["wayland"],
        },
        build=_build_wayland,
    ),
    "wayland-protocols": Recipe(
        name="wayland-protocols",
        deps_for=lambda _s: ("wayland",),
        signature_for=lambda _p, _l: {
            "options": _recipe_list("wayland-protocols"),
            "opt": OPTIMIZATION_TIERS["wayland-protocols"],
        },
        build=_build_wayland_protocols,
    ),
    "shaderc": Recipe(
        name="shaderc",
        deps_for=_deps_none,
        extra_inputs_for=lambda _s: ("glslang", "spirv-tools", "spirv-headers"),
        signature_for=lambda _p, _l: {
            "options": _recipe_list("shaderc"),
            "opt": OPTIMIZATION_TIERS["shaderc"],
        },
        build=_build_shaderc,
    ),
    "spirv-cross": Recipe(
        name="spirv-cross",
        deps_for=_deps_none,
        signature_for=lambda _p, _l: {
            "options": _recipe_list("spirv-cross"),
            "opt": OPTIMIZATION_TIERS["spirv-cross"],
        },
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
            "options": _recipe_section("libplacebo", "common")
            + (
                _recipe_section("libplacebo", "windows")
                if p.system == "windows"
                else []
            ),
            "opt": OPTIMIZATION_TIERS["libplacebo"],
        },
        build=_build_libplacebo,
        helpers=(_strip_cpp_stdlib_from_pc,),
    ),
    "ffmpeg": Recipe(
        name="ffmpeg",
        deps_for=lambda s: (
            ("zlib-ng", "dav1d", "libxml2")
            + (("nv-codec-headers",) if s in ("linux", "windows") else ())
            + {"android": ("libiconv",), "windows": ("win-iconv",), "linux": ()}[s]
        ),
        signature_for=lambda p, _l: {
            "opt": OPTIMIZATION_TIERS["ffmpeg"],
            "lto": BUILD_CONFIG.toolchain.lto,
            "decoders": list(_ffmpeg_decoders(p.system)),
            "demuxers": list(FFMPEG_DEMUXERS),
            "parsers": list(FFMPEG_PARSERS),
            "bsfs": list(FFMPEG_BSFS),
            "protocols": list(FFMPEG_PROTOCOLS),
            "filters": list(FFMPEG_FILTERS),
            "ffmpeg": list(p.ffmpeg),
        },
        build=_build_ffmpeg,
        helpers=(_ffmpeg_decoders, _ffmpeg_flags, _validate_ffmpeg_components),
    ),
    "mpv": Recipe(
        name="mpv",
        deps_for=lambda s: {
            "windows": (
                "zlib-ng",
                "libass",
                "libplacebo",
                "ffmpeg",
                "uchardet",
                "shaderc",
                "spirv-cross",
                "win-iconv",
                "nv-codec-headers",
            ),
            "linux": (
                "zlib-ng",
                "libass",
                "libplacebo",
                "ffmpeg",
                "uchardet",
                "nv-codec-headers",
                "wayland",
                "wayland-protocols",
            ),
            "android": (
                "zlib-ng",
                "libass",
                "libplacebo",
                "ffmpeg",
                "uchardet",
                "libiconv",
            ),
        }[s],
        signature_for=lambda p, _l: {
            "common": _recipe_list("mpv"),
            "mpv": list(p.mpv),
            "system": p.system,
            "library": p.library,
            "opt": OPTIMIZATION_TIERS["mpv"],
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
        "../build-config.json",
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
