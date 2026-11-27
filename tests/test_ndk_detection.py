from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from libmpv_build import builder, toolchains
from libmpv_build.config import load_lock


class NdkDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = load_lock()
        self.revision = self.lock.toolchains["android"].raw["revision"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def make_ndk(self, path: Path, revision: str | None = None) -> Path:
        path.mkdir(parents=True)
        (path / "source.properties").write_text(
            f"Pkg.Revision = {revision or self.revision}\n", encoding="utf-8"
        )
        return path.resolve()

    def run_container(
        self, targets: list[str], *, build_all: bool = False, ndk: str | None = None
    ) -> list[str]:
        with (
            mock.patch("libmpv_build.builder.require_executable"),
            mock.patch(
                "libmpv_build.builder.build_container_image",
                return_value="libmpv-build:test",
            ),
            mock.patch("libmpv_build.builder.run_cmd") as run,
        ):
            builder.run_in_container(
                self.lock,
                targets,
                build_all=build_all,
                jobs=2,
                ndk=ndk,
                toolchain=None,
                verify_cache=False,
                no_clean_dist=False,
                root=self.root,
            )
        run.assert_called_once()
        return run.call_args.args[0]

    def test_local_candidates_follow_environment_priority(self) -> None:
        ndk_root = self.make_ndk(self.root / "ndk-root")
        ndk_home = self.make_ndk(self.root / "ndk-home")
        android_home = self.root / "android-home"
        sdk_root = self.root / "sdk-root"
        home_ndk = self.make_ndk(android_home / "ndk" / self.revision)
        sdk_ndk = self.make_ndk(sdk_root / "ndk" / self.revision)
        candidates = [
            ("ANDROID_NDK_ROOT", str(ndk_root), ndk_root),
            ("ANDROID_NDK_HOME", str(ndk_home), ndk_home),
            ("ANDROID_HOME", str(android_home), home_ndk),
            ("ANDROID_SDK_ROOT", str(sdk_root), sdk_ndk),
        ]
        for index, (variable, _, expected) in enumerate(candidates):
            with self.subTest(variable=variable):
                environment = {name: value for name, value, _ in candidates[index:]}
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(
                        toolchains.find_local_android_ndk(self.lock), expected
                    )

    def test_missing_or_incompatible_candidates_do_not_hide_sdk_ndk(self) -> None:
        sdk = self.root / "sdk"
        expected = self.make_ndk(sdk / "ndk" / self.revision)
        old = self.make_ndk(self.root / "old", revision="29.0.0")
        malformed = self.root / "malformed"
        malformed.mkdir()
        (malformed / "source.properties").write_text("Pkg.Desc = Android NDK\n")
        for candidate in (self.root / "missing", old, malformed):
            with (
                self.subTest(candidate=candidate.name),
                mock.patch.dict(
                    os.environ,
                    {"ANDROID_NDK_ROOT": str(candidate), "ANDROID_HOME": str(sdk)},
                    clear=True,
                ),
            ):
                self.assertEqual(toolchains.find_local_android_ndk(self.lock), expected)

    def test_unreadable_metadata_does_not_hide_sdk_ndk(self) -> None:
        unreadable = self.make_ndk(self.root / "unreadable")
        sdk = self.root / "sdk"
        expected = self.make_ndk(sdk / "ndk" / self.revision)
        read_text = Path.read_text

        def read_properties(path: Path, *args, **kwargs) -> str:
            if path == unreadable / "source.properties":
                raise PermissionError("unreadable NDK metadata")
            return read_text(path, *args, **kwargs)

        with (
            mock.patch.dict(
                os.environ,
                {"ANDROID_NDK_ROOT": str(unreadable), "ANDROID_HOME": str(sdk)},
                clear=True,
            ),
            mock.patch.object(
                Path, "read_text", autospec=True, side_effect=read_properties
            ),
        ):
            self.assertEqual(toolchains.find_local_android_ndk(self.lock), expected)

    def test_sdk_environment_expands_home_directory(self) -> None:
        expected = self.make_ndk(self.root / "sdk home/ndk" / self.revision)
        with mock.patch.dict(
            os.environ, {"HOME": str(self.root), "ANDROID_HOME": "~/sdk home"}
        ):
            self.assertEqual(toolchains.find_local_android_ndk(self.lock), expected)

    def test_explicit_ndk_takes_priority_over_environment(self) -> None:
        explicit = self.make_ndk(self.root / "explicit")
        environment = self.make_ndk(self.root / "environment")
        with mock.patch.dict(os.environ, {"ANDROID_NDK_ROOT": str(environment)}):
            self.assertEqual(
                toolchains.find_local_android_ndk(self.lock, str(explicit)), explicit
            )

    def test_invalid_explicit_ndk_fails_instead_of_using_environment(self) -> None:
        environment = self.make_ndk(self.root / "environment")
        old = self.make_ndk(self.root / "old", revision="29.0.0")
        with mock.patch.dict(os.environ, {"ANDROID_NDK_ROOT": str(environment)}):
            for candidate in (self.root / "missing", old):
                with (
                    self.subTest(candidate=candidate.name),
                    self.assertRaises(RuntimeError),
                ):
                    toolchains.find_local_android_ndk(self.lock, str(candidate))

    def test_local_lookup_without_a_match_never_downloads(self) -> None:
        with mock.patch("libmpv_build.toolchains.restore_toolchain_archive") as restore:
            self.assertIsNone(toolchains.find_local_android_ndk(self.lock))
        restore.assert_not_called()

    def test_resolver_reuses_sdk_ndk_without_download(self) -> None:
        sdk = self.root / "sdk"
        expected = self.make_ndk(sdk / "ndk" / self.revision)
        with (
            mock.patch.dict(os.environ, {"ANDROID_SDK_ROOT": str(sdk)}),
            mock.patch("libmpv_build.toolchains.restore_toolchain_archive") as restore,
        ):
            self.assertEqual(
                toolchains.resolve_android_ndk(self.lock, None, self.root / "cache"),
                expected,
            )
        restore.assert_not_called()

    def test_resolver_downloads_locked_archive_when_no_local_ndk_matches(self) -> None:
        archive_root = self.root / "archive"
        expected = self.make_ndk(archive_root / "android-ndk-r30")
        cache = self.root / "cache"
        with mock.patch(
            "libmpv_build.toolchains.restore_toolchain_archive",
            return_value=archive_root,
        ) as restore:
            self.assertEqual(
                toolchains.resolve_android_ndk(
                    self.lock, None, cache, verify_cache=True
                ),
                expected,
            )
        restore.assert_called_once_with(
            self.lock.toolchains["android"].raw, cache, verify_cache=True
        )

    def test_android_container_mounts_sdk_ndk_read_only_with_spaces_in_path(
        self,
    ) -> None:
        sdk = self.root / "Android SDK"
        expected = self.make_ndk(sdk / "ndk" / self.revision)
        for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            with self.subTest(variable=variable):
                with mock.patch.dict(os.environ, {variable: str(sdk)}, clear=True):
                    command = self.run_container(["android-arm64-v8a"])
                self.assertIn(f"{expected}:{expected}:ro", command)
                self.assertEqual(command[command.index("--ndk") + 1], str(expected))

    def test_all_targets_container_mounts_auto_detected_ndk(self) -> None:
        expected = self.make_ndk(self.root / "ndk")
        with mock.patch.dict(os.environ, {"ANDROID_NDK_ROOT": str(expected)}):
            command = self.run_container(
                ["android-arm64-v8a", "android-x86_64", "windows-x64", "linux-x64"],
                build_all=True,
            )
        self.assertIn("--all", command)
        self.assertIn(f"{expected}:{expected}:ro", command)
        self.assertEqual(command[command.index("--ndk") + 1], str(expected))

    def test_non_android_container_does_not_probe_local_ndk(self) -> None:
        for target in ("linux-x64", "windows-x64"):
            with self.subTest(target=target):
                with mock.patch("libmpv_build.builder.find_local_android_ndk") as find:
                    command = self.run_container([target])
                find.assert_not_called()
                self.assertNotIn("--ndk", command)

    def test_explicit_container_ndk_overrides_sdk_auto_detection(self) -> None:
        expected = self.make_ndk(self.root / "explicit ndk")
        with mock.patch("libmpv_build.builder.find_local_android_ndk") as find:
            command = self.run_container(["android-x86_64"], ndk=str(expected))
        find.assert_not_called()
        self.assertIn(f"{expected}:{expected}:ro", command)
        self.assertEqual(command[command.index("--ndk") + 1], str(expected))

    def test_container_without_local_ndk_keeps_container_download_fallback(
        self,
    ) -> None:
        with mock.patch("libmpv_build.toolchains.restore_toolchain_archive") as restore:
            command = self.run_container(["android-x86_64"])
        self.assertNotIn("--ndk", command)
        restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
