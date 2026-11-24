from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

from libmpv_build.builder import (
    STAGE_COMPLETE,
    _stage_is_valid,
    assemble_prefix,
)
from libmpv_build.config import load_lock, load_profiles
from libmpv_build.fs import capture_tree_manifest, sha256_file
from libmpv_build.recipes import dependency_identity
from libmpv_build.runners import extract_staged_prefix, write_toolchain_files
from libmpv_build.toolchains import (
    ToolchainContext,
    _max_source_date_epoch,
    resolve_android_ndk,
    restore_toolchain_archive,
)


class BuilderTests(unittest.TestCase):
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
                second = replace(lock.dependencies["zlib"], date=second_date)
                candidate = replace(lock, dependencies={"mpv": first, "zlib": second})
                self.assertEqual(_max_source_date_epoch(candidate), expected)

    def test_dependency_identity_tracks_process_execution_helper_changes(self) -> None:
        lock = load_lock()
        profile = load_profiles()["linux-x64"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(
                Path(__file__).parents[1] / "libmpv_build", root / "libmpv_build"
            )
            shutil.copy2(Path(__file__).parents[1] / "Dockerfile", root / "Dockerfile")

            def identity() -> str:
                return dependency_identity(
                    "zlib",
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
