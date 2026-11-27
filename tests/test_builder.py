from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

from libmpv_build.builder import (
    STAGE_COMPLETE,
    TargetBuilder,
    _stage_is_valid,
    assemble_prefix,
    container_image_identity,
    run_in_container,
)
from libmpv_build.config import load_lock, load_profiles
from libmpv_build.fs import capture_tree_manifest, sha256_file
from libmpv_build.recipes import dependency_identity
from libmpv_build.runners import extract_staged_prefix, run_cmake, write_toolchain_files
from libmpv_build.toolchains import (
    ToolchainContext,
    _max_source_date_epoch,
    resolve_android_ndk,
    restore_toolchain_archive,
)


class BuilderTests(unittest.TestCase):
    def test_container_image_identity_tracks_locked_tool_versions(self) -> None:
        lock = load_lock()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Dockerfile").write_text("FROM ubuntu:22.04\n")
            (root / "dependencies.json").write_text(json.dumps(lock.raw))
            image_tags = []
            for llvm in ("21", "20"):
                candidate = json.loads(json.dumps(lock.raw))
                candidate["toolchains"]["linux"]["llvm"] = llvm
                path = root / "dependencies.json"
                path.write_text(json.dumps(candidate))
                with (
                    mock.patch("libmpv_build.builder.require_executable"),
                    mock.patch("libmpv_build.builder.run_cmd") as run,
                ):
                    run_in_container(
                        load_lock(path),
                        ["linux-x64"],
                        build_all=False,
                        jobs=1,
                        ndk=None,
                        toolchain=None,
                        verify_cache=False,
                        no_clean_dist=False,
                        root=root,
                    )
                build_command = run.call_args_list[0].args[0]
                image_tags.append(build_command[build_command.index("-t") + 1])
                self.assertIn(image_tags[-1], run.call_args_list[1].args[0])
            self.assertNotEqual(*image_tags)

    def test_container_build_reuses_only_the_matching_prepared_image(self) -> None:
        lock = load_lock()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Dockerfile").write_text("FROM ubuntu:22.04\n")
            (root / "dependencies.json").write_text(json.dumps(lock.raw))
            identity, image = container_image_identity(lock, root)
            with (
                mock.patch("libmpv_build.builder.require_executable"),
                mock.patch(
                    "libmpv_build.builder.capture_cmd", return_value=identity
                ) as inspect,
                mock.patch("libmpv_build.builder.run_cmd") as run,
            ):
                run_in_container(
                    lock,
                    ["linux-x64"],
                    build_all=False,
                    jobs=1,
                    ndk=None,
                    toolchain=None,
                    verify_cache=False,
                    no_clean_dist=False,
                    reuse_image=True,
                    root=root,
                )
            inspect.assert_called_once()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0:3], ["docker", "run", "--rm"])
            self.assertIn(image, run.call_args.args[0])

    def test_container_build_requires_matching_image_when_reuse_is_requested(self) -> None:
        lock = load_lock()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Dockerfile").write_text("FROM ubuntu:22.04\n")
            (root / "dependencies.json").write_text(json.dumps(lock.raw))
            with (
                mock.patch("libmpv_build.builder.require_executable"),
                mock.patch(
                    "libmpv_build.builder.capture_cmd",
                    side_effect=subprocess.CalledProcessError(1, ["docker"]),
                ),
                mock.patch("libmpv_build.builder.run_cmd") as run,
                self.assertRaisesRegex(RuntimeError, "container-image"),
            ):
                run_in_container(
                    lock,
                    ["linux-x64"],
                    build_all=False,
                    jobs=1,
                    ndk=None,
                    toolchain=None,
                    verify_cache=False,
                    no_clean_dist=False,
                    reuse_image=True,
                    root=root,
                )
            run.assert_not_called()

    def test_container_build_rejects_image_with_a_stale_identity_label(self) -> None:
        lock = load_lock()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Dockerfile").write_text("FROM ubuntu:22.04\n")
            (root / "dependencies.json").write_text(json.dumps(lock.raw))
            with (
                mock.patch("libmpv_build.builder.require_executable"),
                mock.patch("libmpv_build.builder.capture_cmd", return_value="stale"),
                mock.patch("libmpv_build.builder.run_cmd") as run,
                self.assertRaisesRegex(RuntimeError, "does not match the current lock"),
            ):
                run_in_container(
                    lock,
                    ["linux-x64"],
                    build_all=False,
                    jobs=1,
                    ndk=None,
                    toolchain=None,
                    verify_cache=False,
                    no_clean_dist=False,
                    reuse_image=True,
                    root=root,
                )
            run.assert_not_called()

    def test_source_date_epoch_compares_instants_across_timezones(self) -> None:
        lock = load_lock()
        expected = str(
            int(datetime.fromisoformat("2026-01-02T07:00:00+00:00").timestamp())
        )
        for first_date, second_date in (
            ("2026-01-01T23:00:00-08:00", "2026-01-02T01:00:00+01:00"),
            ("2026-01-02T07:00:00Z", "2026-01-02T08:00:00+01:00"),
            ("2026-01-02T07:00:00Z", "2026-01-02T01:00:00+01:00"),
            ("2026-01-02T00:00:00Z", "2026-01-01T23:00:00-08:00"),
        ):
            with self.subTest(first=first_date, second=second_date):
                first = replace(lock.dependencies["mpv"], date=first_date)
                second = replace(lock.dependencies["zlib-ng"], date=second_date)
                candidate = replace(lock, dependencies={"mpv": first, "zlib-ng": second})
                self.assertEqual(_max_source_date_epoch(candidate), expected)

    def test_dependency_identity_tracks_process_execution_helper_changes(self) -> None:
        lock = load_lock()
        profile = load_profiles()["linux-x64"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(
                Path(__file__).parents[1] / "libmpv_build", root / "libmpv_build"
            )
            shutil.copy2(
                Path(__file__).parents[1] / "build-config.json",
                root / "build-config.json",
            )
            shutil.copy2(Path(__file__).parents[1] / "Dockerfile", root / "Dockerfile")

            def identity() -> str:
                return dependency_identity(
                    "zlib-ng",
                    lock=lock,
                    profile=profile,
                    compiler_version="clang fixture",
                    upstream_ids={},
                    root=root,
                )

            original = identity()
            process = root / "libmpv_build/process.py"
            process.write_text(
                process.read_text(encoding="utf-8") + "\n# changed subprocess policy\n",
                encoding="utf-8",
            )
            self.assertNotEqual(original, identity())

    def test_android_cmake_toolchain_declares_android_platform_and_api(self) -> None:
        profile = load_profiles()["android-arm64-v8a"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tc = ToolchainContext(
                profile=profile,
                toolroot=root / "ndk",
                prefix=root / "prefix",
                cc="aarch64-linux-android28-clang",
                cxx="aarch64-linux-android28-clang++",
                ar="llvm-ar",
                ranlib="llvm-ranlib",
                nm="llvm-nm",
                strip="llvm-strip",
                objcopy="llvm-objcopy",
                windres=None,
                compiler_version="clang fixture",
                cflags=[],
                cxxflags=[],
                ldflags=[],
                env={},
            )
            with mock.patch(
                "libmpv_build.runners.require_executable", side_effect=lambda name: name
            ):
                _, _, cmake_file = write_toolchain_files(tc, root)
            cmake = cmake_file.read_text(encoding="utf-8")
            self.assertIn('set(CMAKE_SYSTEM_NAME "Android")', cmake)
            self.assertIn('set(CMAKE_SYSTEM_VERSION "28")', cmake)

    def test_cross_native_file_uses_locked_llvm_version_instead_of_hardcoded_fallback(
        self,
    ) -> None:
        profile = load_profiles()["android-arm64-v8a"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tc = ToolchainContext(
                profile=profile,
                toolroot=root / "ndk",
                prefix=root / "prefix",
                cc="aarch64-linux-android28-clang",
                cxx="aarch64-linux-android28-clang++",
                ar="llvm-ar",
                ranlib="llvm-ranlib",
                nm="llvm-nm",
                strip="llvm-strip",
                objcopy="llvm-objcopy",
                windres=None,
                compiler_version="clang fixture",
                cflags=[],
                cxxflags=[],
                ldflags=[],
                env={},
                llvm_version="22",
            )
            with (
                mock.patch("libmpv_build.runners.shutil.which", return_value=None),
                mock.patch(
                    "libmpv_build.runners.require_executable", side_effect=lambda name: name
                ),
            ):
                _, native_file, _ = write_toolchain_files(tc, root)
            native = native_file.read_text(encoding="utf-8")
            self.assertIn("clang-22", native)
            self.assertIn("-fuse-ld=lld-22", native)
            self.assertIn("llvm-ar-22", native)
            self.assertNotIn("clang-21", native)

    def test_builder_rejects_mismatched_locked_build_tools(self) -> None:
        lock = load_lock()
        profile = load_profiles()["linux-x64"]
        expected = {
            "meson": lock.build_tools["meson"]["version"],
            "ninja": lock.build_tools["ninja"]["version"],
            "cmake": f"cmake version {lock.build_tools['cmake']['version']}\nCMake suite maintained and supported by Kitware.",
        }
        for bad_tool, bad_output in (
            ("meson", "1.0.0"),
            ("ninja", "1.10.1"),
            ("cmake", "cmake version 3.22.1\nCMake suite."),
        ):
            with self.subTest(bad_tool=bad_tool), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                shutil.copy2(
                    Path(__file__).parents[1] / "build-config.json",
                    root / "build-config.json",
                )

                def fake_capture(
                    cmd: list[str],
                    *,
                    bad_tool: str = bad_tool,
                    bad_output: str = bad_output,
                ) -> str:
                    tool = cmd[0]
                    return bad_output if tool == bad_tool else expected[tool]

                builder = TargetBuilder(lock, profile, jobs=1, root=root)
                with (
                    mock.patch("libmpv_build.builder._validate_loaded_build_config"),
                    mock.patch("libmpv_build.builder._build_inputs_snapshot", return_value={}),
                    mock.patch("libmpv_build.builder.capture_cmd", side_effect=fake_capture),
                    self.assertRaisesRegex(RuntimeError, bad_tool.capitalize() if bad_tool != "cmake" else "CMake"),
                ):
                    builder.build()

        kitware_expected = {
            **expected,
            "ninja": f"{lock.build_tools['ninja']['version']}.git.kitware.jobserver-pipe-1",
        }
        builder = TargetBuilder(lock, profile, jobs=1, root=Path(__file__).parents[1])
        with mock.patch(
            "libmpv_build.builder.capture_cmd",
            side_effect=lambda cmd: kitware_expected[cmd[0]],
        ):
            builder._verify_locked_build_tools()

    def test_toolchain_context_tiered_flags_and_full_lto_files(self) -> None:
        profile = load_profiles()["linux-x64"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tc = ToolchainContext(
                profile=profile,
                toolroot=None,
                prefix=root / "prefix",
                cc="clang-21",
                cxx="clang++-21",
                ar="llvm-ar-21",
                ranlib="llvm-ranlib-21",
                nm="llvm-nm-21",
                strip="llvm-strip-21",
                objcopy="llvm-objcopy-21",
                windres=None,
                compiler_version="clang 21",
                cflags=["-Oz", "-march=x86-64-v2", "-flto=full", "-fvisibility=hidden"],
                cxxflags=[
                    "-Oz",
                    "-march=x86-64-v2",
                    "-flto=full",
                    "-fvisibility=hidden",
                    "-fvisibility-inlines-hidden",
                ],
                ldflags=["-fuse-ld=lld-21", "-flto=full", "-Wl,--lto-O2"],
                env={"CFLAGS": "-Oz", "CXXFLAGS": "-Oz"},
                llvm_version="21",
            )
            self.assertEqual(tc.cflags_for("O3")[0], "-O3")
            self.assertNotIn("-Oz", tc.cflags_for("O3"))
            self.assertNotIn(
                "-fvisibility=hidden", tc.cflags_for("O2", hidden_visibility=False)
            )
            self.assertNotIn(
                "-fvisibility-inlines-hidden",
                tc.cxxflags_for("O2", hidden_visibility=False),
            )
            with mock.patch(
                "libmpv_build.runners.require_executable", side_effect=lambda name: name
            ):
                cross_file, native_file, cmake_file = write_toolchain_files(tc, root)
            cross = cross_file.read_text(encoding="utf-8")
            native = native_file.read_text(encoding="utf-8")
            cmake = cmake_file.read_text(encoding="utf-8")
            self.assertIn("b_lto = false", cross)
            self.assertNotIn("b_lto_mode", cross)
            self.assertIn("-flto=full", cross)
            self.assertIn("buildtype = 'custom'", cross)
            self.assertNotIn("debug =", cross)
            self.assertNotIn("optimization =", cross)
            self.assertIn(f"-L{root / 'prefix' / 'lib'}", cross)
            self.assertIn("clang-21", native)
            self.assertIn("-fuse-ld=lld-21", native)
            self.assertIn("-flto=full", cmake)
            self.assertNotIn("-Oz", cmake)

    def test_cmake_extra_cxxflags_follow_platform_initial_flags(self) -> None:
        profile = load_profiles()["android-arm64-v8a"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tc = ToolchainContext(
                profile=profile,
                toolroot=root / "ndk",
                prefix=root / "prefix",
                cc="aarch64-linux-android28-clang",
                cxx="aarch64-linux-android28-clang++",
                ar="llvm-ar",
                ranlib="llvm-ranlib",
                nm="llvm-nm",
                strip="llvm-strip",
                objcopy="llvm-objcopy",
                windres=None,
                compiler_version="clang fixture",
                cflags=["-Oz", "-march=armv8-a", "-flto=full"],
                cxxflags=["-Oz", "-march=armv8-a", "-flto=full"],
                ldflags=[],
                env={},
            )
            with mock.patch("libmpv_build.runners.run_cmd") as run:
                run_cmake(
                    tc,
                    root,
                    root / "source",
                    root / "build",
                    root / "destdir",
                    1,
                    extra_cxxflags=("-fno-exceptions", "-fno-rtti"),
                )
            configure = run.call_args_list[0].args[0]
            cxx_init = next(
                argument
                for argument in configure
                if isinstance(argument, str)
                and argument.startswith("-DCMAKE_CXX_FLAGS_INIT=")
            )
            cxx_minsize = next(
                argument
                for argument in configure
                if isinstance(argument, str)
                and argument.startswith("-DCMAKE_CXX_FLAGS_MINSIZEREL=")
            )
            self.assertNotIn("-fno-exceptions", cxx_init)
            self.assertNotIn("-fno-rtti", cxx_init)
            self.assertEqual(
                cxx_minsize,
                "-DCMAKE_CXX_FLAGS_MINSIZEREL=-fno-exceptions -fno-rtti -DNDEBUG",
            )
            self.assertIn("-Oz", cxx_init)
            self.assertIn("-flto=full", cxx_init)

    def test_stage_cache_rejects_missing_or_changed_outputs_and_strict_tampering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            output = stage / "lib.a"
            output.write_bytes(b"archive")
            original = output.stat()
            marker = {
                "dependency": "library",
                "identity": "identity",
                "manifest": capture_tree_manifest(stage),
                "hashes": {"lib.a": sha256_file(output)},
            }
            (stage / STAGE_COMPLETE).write_text(json.dumps(marker), encoding="utf-8")
            self.assertTrue(_stage_is_valid(stage, "library", "identity", strict=False))
            self.assertTrue(_stage_is_valid(stage, "library", "identity", strict=True))
            output.write_bytes(b"changed")
            os.utime(output, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertTrue(_stage_is_valid(stage, "library", "identity", strict=False))
            self.assertFalse(_stage_is_valid(stage, "library", "identity", strict=True))
            output.unlink()
            self.assertFalse(
                _stage_is_valid(stage, "library", "identity", strict=False)
            )

    def test_stage_cache_rejects_legacy_and_invalid_markers(self) -> None:
        for marker in ([], {"dependency": "library", "identity": "identity"}):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as td:
                stage = Path(td)
                (stage / STAGE_COMPLETE).write_text(
                    json.dumps(marker), encoding="utf-8"
                )
                self.assertFalse(
                    _stage_is_valid(stage, "library", "identity", strict=False)
                )

    def test_missing_stage_or_destdir_fails_without_replacing_good_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prefix = root / "prefix"
            prefix.mkdir()
            (prefix / "old.a").write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "stage is missing"):
                assemble_prefix([root / "missing"], prefix)
            self.assertEqual((prefix / "old.a").read_bytes(), b"old")
            with self.assertRaisesRegex(RuntimeError, "DESTDIR prefix"):
                extract_staged_prefix(root / "destdir", prefix, root / "stage")

    def test_prefix_rebases_absolute_stage_links_and_never_writes_through_aliases(
        self,
    ) -> None:
        for absolute in (False, True):
            with self.subTest(absolute=absolute), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                first, second, prefix = root / "first", root / "second", root / "prefix"
                first.mkdir()
                second.mkdir()
                original = first / "cache.a"
                original.write_bytes(b"immutable")
                (first / "lib.a").symlink_to(original if absolute else Path("cache.a"))
                assemble_prefix([first], prefix)
                self.assertFalse((prefix / "lib.a").readlink().is_absolute())
                (prefix / "lib.a").write_bytes(b"prefix only")
                self.assertEqual(original.read_bytes(), b"immutable")
                (second / "lib.a").write_bytes(b"replacement")
                assemble_prefix([first, second], prefix)
                self.assertFalse((prefix / "lib.a").is_symlink())
                self.assertEqual((prefix / "lib.a").read_bytes(), b"replacement")
                self.assertEqual((prefix / "cache.a").read_bytes(), b"immutable")
                self.assertEqual(original.read_bytes(), b"immutable")

    def test_prefix_rejects_symlink_parents_and_escaping_links_without_replacing_old_tree(
        self,
    ) -> None:
        for case in ("parent", "escape", "stage"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                first, second, prefix = root / "first", root / "second", root / "prefix"
                first.mkdir()
                second.mkdir()
                prefix.mkdir()
                (prefix / "old.a").write_bytes(b"previous")
                (first / "real").mkdir()
                (first / "real/cache.a").write_bytes(b"immutable")
                if case == "parent":
                    (first / "alias").symlink_to("real", target_is_directory=True)
                    (second / "alias").mkdir()
                    (second / "alias/cache.a").write_bytes(b"replacement")
                elif case == "escape":
                    (first / "alias").symlink_to(prefix / "old.a")
                else:
                    (root / "alias").symlink_to(first, target_is_directory=True)
                    first = root / "alias"
                with self.assertRaises(ValueError):
                    assemble_prefix([first, second], prefix)
                self.assertEqual((prefix / "old.a").read_bytes(), b"previous")
                self.assertEqual(
                    (root / "first/real/cache.a").read_bytes(), b"immutable"
                )

    def test_toolchain_cache_recovers_from_malformed_markers(self) -> None:
        for invalid_marker in ([], {"schemaVersion": 2}):
            with (
                self.subTest(marker=invalid_marker),
                tempfile.TemporaryDirectory() as td,
            ):
                root = Path(td)
                toolchains = root / "toolchains"
                toolchains.mkdir()
                archive = root / "input.tar.xz"
                with tarfile.open(archive, "w:xz") as package:
                    info = tarfile.TarInfo("tool/file")
                    info.size = 3
                    package.addfile(info, io.BytesIO(b"new"))
                digest = sha256_file(archive)
                archive.replace(toolchains / f"{digest}.tar.xz")
                stage = toolchains / digest
                stage.mkdir()
                invalid_marker = (
                    dict(invalid_marker, sha256=digest)
                    if isinstance(invalid_marker, dict)
                    else invalid_marker
                )
                (stage / STAGE_COMPLETE).write_text(
                    json.dumps(invalid_marker), encoding="utf-8"
                )
                record = {"url": "https://example.com/tool.tar.xz", "sha256": digest}
                actual = restore_toolchain_archive(record, root)
                self.assertEqual((actual / "tool/file").read_bytes(), b"new")

    def test_ndk_checks_both_sdk_roots(self) -> None:
        lock = load_lock()
        revision = lock.toolchains["android"].raw["revision"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ndk = root / "sdk/ndk" / revision
            ndk.mkdir(parents=True)
            (ndk / "source.properties").write_text(
                f"Pkg.Revision = {revision}\n", encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ,
                {
                    "ANDROID_HOME": str(root / "empty"),
                    "ANDROID_SDK_ROOT": str(root / "sdk"),
                },
                clear=True,
            ):
                self.assertEqual(
                    resolve_android_ndk(lock, None, root / "cache"), ndk.resolve()
                )


if __name__ == "__main__":
    unittest.main()
