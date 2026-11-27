"""Meson, CMake, and Autotools build runners with DESTDIR-isolated staging."""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from .config import BUILD_CONFIG
from .process import require_executable, run_cmd
from .toolchains import ToolchainContext


def write_toolchain_files(
    tc: ToolchainContext, base_dir: Path
) -> tuple[Path, Path, Path]:
    cross_file = base_dir / "cross.ini"
    native_file = base_dir / "native.ini"
    cmake_file = base_dir / "toolchain.cmake"

    bins = {
        "c": tc.cc,
        "cpp": tc.cxx,
        "ar": tc.ar,
        "nm": tc.nm,
        "strip": tc.strip,
        "pkg-config": require_executable("pkg-config"),
    }
    if tc.profile.system == "windows" and tc.windres:
        bins["windres"] = tc.windres

    meson_cpp_args = [flag for flag in tc.cxxflags if flag != "-static-libstdc++"]
    meson_link_args = tc.ldflags + [f"-L{tc.prefix}/lib"]
    meson_cpp_link_args = meson_link_args + (
        ["-static-libstdc++"] if tc.profile.system in ("android", "windows") else []
    )

    lines = ["[binaries]"] + [f"{name} = {value!r}" for name, value in bins.items()]
    lines += [
        "[host_machine]",
        f"system = '{tc.profile.system}'",
        f"cpu_family = '{tc.profile.cpu}'",
        f"cpu = '{tc.profile.cpu}'",
        "endian = 'little'",
        "[built-in options]",
        "default_library = 'static'",
        f"b_staticpic = {'false' if tc.profile.system == 'windows' else 'true'}",
        "buildtype = 'custom'",
        "b_lto = false",
        "wrap_mode = 'nodownload'",
        f"c_args = {tc.cflags!r}",
        f"cpp_args = {meson_cpp_args!r}",
        f"c_link_args = {meson_link_args!r}",
        f"cpp_link_args = {meson_cpp_link_args!r}",
    ]
    cross_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    llvm_suffix = f"-{tc.llvm_version}" if tc.llvm_version else ""
    if tc.profile.system == "linux":
        native_cc = require_executable(f"clang{llvm_suffix}")
        native_cxx = require_executable(f"clang++{llvm_suffix}")
        native_linker = f"lld{llvm_suffix}"
        native_ar = require_executable(f"llvm-ar{llvm_suffix}")
    else:
        native_versioned = not shutil.which("clang")
        fallback_suffix = (
            f"-{tc.llvm_version}" if (native_versioned and tc.llvm_version) else ""
        )
        native_cc = require_executable(f"clang{fallback_suffix}")
        native_cxx = require_executable(f"clang++{fallback_suffix}")
        native_linker = f"lld{fallback_suffix}"
        native_ar = require_executable(f"llvm-ar{fallback_suffix}")
    native_file.write_text(
        "\n".join(
            [
                "[binaries]",
                f"c = {native_cc!r}",
                f"cpp = {native_cxx!r}",
                f"ar = {native_ar!r}",
                "[built-in options]",
                "b_lto = false",
                "c_args = []",
                "cpp_args = []",
                f"c_link_args = ['-fuse-ld={native_linker}']",
                f"cpp_link_args = ['-fuse-ld={native_linker}']",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cmake_c_flags = [flag for flag in tc.cflags if not flag.startswith("-O")]
    cmake_cxx_flags = [
        flag
        for flag in tc.cxxflags
        if not flag.startswith("-O") and flag != "-static-libstdc++"
    ]
    cmake = [
        f'set(CMAKE_C_COMPILER "{tc.cc}")',
        f'set(CMAKE_CXX_COMPILER "{tc.cxx}")',
        f'set(CMAKE_AR "{tc.ar}")',
        f'set(CMAKE_RANLIB "{tc.ranlib}")',
        "if(NOT DEFINED CMAKE_C_FLAGS_INIT)",
        f'    set(CMAKE_C_FLAGS_INIT "{shlex.join(cmake_c_flags)}")',
        "endif()",
        "if(NOT DEFINED CMAKE_CXX_FLAGS_INIT)",
        f'    set(CMAKE_CXX_FLAGS_INIT "{shlex.join(cmake_cxx_flags)}")',
        "endif()",
        f'set(CMAKE_EXE_LINKER_FLAGS_INIT "{shlex.join(tc.ldflags)}")',
        f'set(CMAKE_SHARED_LINKER_FLAGS_INIT "{shlex.join(tc.ldflags)}")',
        'set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)',
        f'set(CMAKE_FIND_ROOT_PATH "{tc.prefix}")',
    ]
    if tc.profile.system != "windows":
        cmake.append("set(CMAKE_POSITION_INDEPENDENT_CODE ON)")
    for language in ("C", "CXX"):
        cmake += [
            f'set(CMAKE_{language}_COMPILER_AR "{tc.ar}")',
            f'set(CMAKE_{language}_COMPILER_RANLIB "{tc.ranlib}")',
        ]
    if tc.profile.system != "linux":
        sys_name = "Windows" if tc.profile.system == "windows" else "Android"
        cmake += [
            f'set(CMAKE_SYSTEM_NAME "{sys_name}")',
            f'set(CMAKE_SYSTEM_PROCESSOR "{tc.profile.cpu}")',
            "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)",
        ]
        if tc.profile.system == "android":
            if tc.profile.api is None:
                raise ValueError("Android target profile must declare an API level")
            abi = {"aarch64": "arm64-v8a", "x86_64": "x86_64"}.get(tc.profile.cpu)
            if abi is None:
                raise ValueError(f"Unsupported Android CPU ABI: {tc.profile.cpu}")
            cmake += [
                f'set(CMAKE_SYSTEM_VERSION "{tc.profile.api}")',
                f'set(CMAKE_ANDROID_API "{tc.profile.api}")',
                f'set(CMAKE_ANDROID_ARCH_ABI "{abi}")',
                f'set(CMAKE_ANDROID_NDK "{tc.toolroot}")'
                if tc.toolroot is not None
                else "",
            ]
    cmake_file.write_text("\n".join(cmake) + "\n", encoding="utf-8")
    return cross_file, native_file, cmake_file


def extract_staged_prefix(destdir: Path, prefix: Path, stage_prefix: Path) -> None:
    staged = destdir / str(prefix).lstrip("/")
    if staged.is_symlink() or not staged.is_dir():
        raise RuntimeError(
            f"Build did not install the expected DESTDIR prefix: {staged}"
        )
    shutil.copytree(staged, stage_prefix, dirs_exist_ok=True, symlinks=True)


def run_meson(
    tc: ToolchainContext,
    base_dir: Path,
    source: Path,
    build_dir: Path,
    destdir: Path,
    jobs: int,
    options: list[str] | tuple[str, ...] = (),
    *,
    shared: bool = False,
    opt: str = "Oz",
    hidden_visibility: bool = True,
    native: bool = False,
) -> None:
    meson_bin = require_executable("meson")
    cross_file = base_dir / "cross.ini"
    native_file = base_dir / "native.ini"
    c_args = tc.cflags_for(opt, hidden_visibility=hidden_visibility)
    cpp_args = [
        flag
        for flag in tc.cxxflags_for(opt, hidden_visibility=hidden_visibility)
        if flag != "-static-libstdc++"
    ]
    command: list[str | Path] = [
        meson_bin,
        "setup",
        build_dir,
        source,
        "--native-file" if native else "--cross-file",
        cross_file,
        *([] if native else ["--native-file", native_file]),
        "--prefix",
        tc.prefix,
        "--libdir",
        "lib",
        "--wrap-mode=nodownload",
        f"-Dc_args={c_args!r}",
        f"-Dcpp_args={cpp_args!r}",
    ]
    if shared:
        command += ["--default-library=shared", "-Db_lundef=true"]
    command += [f"-D{option}" for option in options]
    command += [f"-D{option}" for option in BUILD_CONFIG.toolchain.meson_options]

    setup_env = tc.env.copy()
    for key in ("CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"):
        setup_env.pop(key, None)
    run_cmd(command, env=setup_env)
    run_cmd(["ninja", "-C", build_dir, f"-j{jobs}"], env=tc.env)
    run_cmd(
        [meson_bin, "install", "-C", build_dir, "--no-rebuild", "--destdir", destdir],
        env=tc.env,
    )


def run_cmake(
    tc: ToolchainContext,
    base_dir: Path,
    source: Path,
    build_dir: Path,
    destdir: Path,
    jobs: int,
    options: list[str] | tuple[str, ...] = (),
    *,
    targets: list[str] | None = None,
    install: bool = True,
    opt: str = "Oz",
    hidden_visibility: bool = True,
    extra_cxxflags: list[str] | tuple[str, ...] = (),
) -> Path:
    cmake_file = base_dir / "toolchain.cmake"
    setup_env = tc.env.copy()
    for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        setup_env.pop(key, None)
    build_type = "Release" if opt in ("O2", "O3") else "MinSizeRel"
    build_tag = build_type.upper()
    c_init = tc.cflags_for(opt, hidden_visibility=hidden_visibility)
    cxx_init = [
        flag
        for flag in tc.cxxflags_for(opt, hidden_visibility=hidden_visibility)
        if flag != "-static-libstdc++"
    ]
    c_build_flag = "" if "-DNDEBUG" in c_init else "-DNDEBUG"
    cxx_build_flags = list(extra_cxxflags)
    if "-DNDEBUG" not in cxx_init and "-DNDEBUG" not in cxx_build_flags:
        cxx_build_flags.append("-DNDEBUG")
    cmake_args: list[str | Path] = [
        "cmake",
        "-S",
        source,
        "-B",
        build_dir,
        "-G",
        "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={cmake_file}",
        f"-DCMAKE_INSTALL_PREFIX={tc.prefix}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF",
        f"-DCMAKE_C_FLAGS_INIT={shlex.join(c_init)}",
        f"-DCMAKE_CXX_FLAGS_INIT={shlex.join(cxx_init)}",
        f"-DCMAKE_C_FLAGS_{build_tag}={c_build_flag}",
        f"-DCMAKE_CXX_FLAGS_{build_tag}={shlex.join(cxx_build_flags)}",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DBUILD_TESTING=OFF",
    ]
    cmake_args.extend(options)
    run_cmd(cmake_args, env=setup_env)
    build_cmd: list[str | Path] = [
        "cmake",
        "--build",
        build_dir,
        "--parallel",
        str(jobs),
    ]
    if targets:
        build_cmd += ["--target", *targets]
    run_cmd(build_cmd, env=tc.env)
    if install:
        run_cmd(
            ["cmake", "--install", build_dir], env={**tc.env, "DESTDIR": str(destdir)}
        )
    return build_dir


def run_autotools(
    tc: ToolchainContext,
    source: Path,
    build_dir: Path,
    destdir: Path,
    jobs: int,
    options: list[str] | tuple[str, ...] = (),
    *,
    opt: str = "Oz",
    hidden_visibility: bool = True,
) -> None:
    build_env = tc.env_for(opt, hidden_visibility=hidden_visibility)
    if not (source / "configure").is_file():
        if (source / "autogen.sh").is_file():
            run_cmd(
                ["sh", source / "autogen.sh"],
                cwd=source,
                env={**build_env, "NOCONFIGURE": "1"},
            )
        else:
            run_cmd(["autoreconf", "-fi"], cwd=source, env=build_env)
    build_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            source / "configure",
            f"--prefix={tc.prefix}",
            f"--host={tc.profile.triple}",
            "--enable-static",
            "--disable-shared",
            "--with-pic",
            *options,
        ],
        cwd=build_dir,
        env=build_env,
    )
    run_cmd(["make", f"-j{jobs}"], cwd=build_dir, env=build_env)
    run_cmd(["make", f"DESTDIR={destdir}", "install"], cwd=build_dir, env=build_env)


def write_pkgconfig(
    stage_prefix: Path,
    name: str,
    library: str,
    version: str,
    *,
    private: str = "",
    extra_cflags: str = "",
) -> None:
    directory = stage_prefix / "lib/pkgconfig"
    directory.mkdir(parents=True, exist_ok=True)
    cflags = "-I${includedir}" + (f" {extra_cflags}" if extra_cflags else "")
    content = (
        "prefix=${pcfiledir}/../..\n"
        "libdir=${prefix}/lib\n"
        "includedir=${prefix}/include\n\n"
        f"Name: {name}\n"
        f"Description: locked static {name}\n"
        f"Version: {version}\n"
        f"Libs: -L${{libdir}} -l{library}\n"
        f"Libs.private: {private}\n"
        f"Cflags: {cflags}\n"
    )
    (directory / f"{name}.pc").write_text(content, encoding="utf-8")
