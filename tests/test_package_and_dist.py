from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from libmpv_build import ROOT
from libmpv_build.builder import TargetBuilder
from libmpv_build.config import load_lock, load_profiles
from libmpv_build.fs import sha256_file
from libmpv_build.package import (
    _delivery_inputs_snapshot,
    _validate_build_info,
    clean_dist,
    create_deterministic_zip,
    package_target,
    promote_files,
)
from libmpv_build.recipes import dependency_identity, resolve_build_order


class PackageAndDistTests(unittest.TestCase):
    def test_changed_loaded_configuration_fails_before_building_or_packaging(self) -> None:
        lock = load_lock()
        profile = load_profiles()["linux-x64"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = json.loads((ROOT / "build-config.json").read_text())
            config["optimizationTiers"]["dav1d"] = "O2"
            (root / "build-config.json").write_text(json.dumps(config))
            builder = TargetBuilder(lock, profile, jobs=1, root=root)
            for action in (
                builder.build,
                lambda: _validate_build_info(lock, profile, root / "prefix", root),
            ):
                with self.subTest(action=action), self.assertRaisesRegex(
                    ValueError, "configuration changed after loading"
                ):
                    action()
            self.assertFalse((root / "build").exists())
            self.assertFalse((root / "prefix").exists())

    def test_clean_dist_preserves_other_targets_for_same_release(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td)
            old_zip = dist / "libmpv-0.42.0-r5-linux-x64.zip"
            keep_zip = dist / "libmpv-0.42.0-r6-windows-x64.zip"
            sel_zip = dist / "libmpv-0.42.0-r6-linux-x64.zip"
            stale_report = dist / "old-report.json"
            for p in (old_zip, keep_zip, sel_zip, stale_report):
                p.write_bytes(b"data")

            clean_dist(dist, "0.42.0-r6", ["linux-x64"])
            self.assertFalse(old_zip.exists())
            self.assertFalse(sel_zip.exists())
            self.assertFalse(stale_report.exists())
            self.assertTrue(keep_zip.exists())
            self.assertTrue((dist / "SHA256SUMS").is_file())
            self.assertIn(
                "libmpv-0.42.0-r6-windows-x64.zip",
                (dist / "SHA256SUMS").read_text(encoding="utf-8"),
            )

            clean_dist(
                dist,
                "0.42.0-r6",
                ["android-arm64-v8a", "android-x86_64", "windows-x64", "linux-x64"],
            )
            self.assertFalse(keep_zip.exists())

    def test_deterministic_zip_and_immutable_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "libmpv.so.2").write_bytes(b"ELF-TEST")
            z1 = tdp / "a.zip"
            z2 = tdp / "b.zip"
            create_deterministic_zip(src, z1, 1700000000)
            create_deterministic_zip(src, z2, 1700000000)
            self.assertEqual(z1.read_bytes(), z2.read_bytes())

            dist = tdp / "dist"
            dist.mkdir()
            promote_files(
                {"libmpv-0.42.0-r6-linux-x64.zip": z1},
                dist,
                {"libmpv-0.42.0-r6-linux-x64.zip"},
            )
            # Same content succeeds; different content raises
            promote_files(
                {"libmpv-0.42.0-r6-linux-x64.zip": z2},
                dist,
                {"libmpv-0.42.0-r6-linux-x64.zip"},
            )
            (src / "libmpv.so.2").write_bytes(b"DIFFERENT")
            z3 = tdp / "c.zip"
            create_deterministic_zip(src, z3, 1700000000)
            with self.assertRaises(ValueError):
                promote_files(
                    {"libmpv-0.42.0-r6-linux-x64.zip": z3},
                    dist,
                    {"libmpv-0.42.0-r6-linux-x64.zip"},
                )

    def test_clean_dist_rolls_back_when_checksums_cannot_be_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td) / "dist"
            dist.mkdir()
            previous = {
                "libmpv-0.42.0-r5-linux-x64.zip": b"old",
                "libmpv-0.42.0-r6-windows-x64.zip": b"keep",
                "SHA256SUMS": b"previous sums\n",
            }
            for name, content in previous.items():
                (dist / name).write_bytes(content)
            with (
                mock.patch(
                    "libmpv_build.package.atomic_write_text",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(OSError),
            ):
                clean_dist(dist, "0.42.0-r6", ["linux-x64"])
            self.assertEqual(
                {p.name: p.read_bytes() for p in dist.iterdir() if p.name != ".lock"},
                previous,
            )

    def test_promotion_rolls_back_library_when_checksum_promotion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dist = root / "dist"
            dist.mkdir()
            (dist / "existing.zip").write_bytes(b"previous")
            (dist / "SHA256SUMS").write_bytes(b"previous sums")
            incoming = root / "incoming.zip"
            incoming.write_bytes(b"new")
            real_replace = Path.replace

            def replace(path: Path, target: Path) -> Path:
                if path.name == "SHA256SUMS" and path.parent.name == "incoming":
                    raise OSError("checksum promotion failed")
                return real_replace(path, target)

            with (
                mock.patch.object(Path, "replace", replace),
                self.assertRaises(OSError),
            ):
                promote_files({"new.zip": incoming}, dist, {"new.zip"})
            self.assertFalse((dist / "new.zip").exists())
            self.assertEqual((dist / "existing.zip").read_bytes(), b"previous")
            self.assertEqual((dist / "SHA256SUMS").read_bytes(), b"previous sums")

    def test_promotion_checksums_describe_the_staged_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            incoming = root / "source.zip"
            incoming.write_bytes(b"snapshot")
            dist = root / "dist"
            real_copy = shutil.copyfile

            def copy(source: Path, destination: Path) -> Path:
                result = real_copy(source, destination)
                if source == incoming:
                    incoming.write_bytes(b"changed after staging")
                return result

            with mock.patch("libmpv_build.package.shutil.copyfile", side_effect=copy):
                promote_files({"new.zip": incoming}, dist, {"new.zip"})
            self.assertEqual((dist / "new.zip").read_bytes(), b"snapshot")
            self.assertEqual(
                (dist / "SHA256SUMS").read_text(),
                f"{sha256_file(dist / 'new.zip')}  new.zip\n",
            )

    def test_immutable_promotion_compares_the_staged_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            incoming = root / "source.zip"
            incoming.write_bytes(b"previous")
            dist = root / "dist"
            dist.mkdir()
            (dist / "existing.zip").write_bytes(b"previous")
            (dist / "SHA256SUMS").write_bytes(b"previous sums")
            real_copy = shutil.copyfile

            def copy(source: Path, destination: Path) -> Path:
                if source == incoming:
                    incoming.write_bytes(b"changed before staging")
                return real_copy(source, destination)

            with (
                mock.patch("libmpv_build.package.shutil.copyfile", side_effect=copy),
                self.assertRaisesRegex(ValueError, "Immutable artifact differs"),
            ):
                promote_files({"existing.zip": incoming}, dist, {"existing.zip"})
            self.assertEqual((dist / "existing.zip").read_bytes(), b"previous")
            self.assertEqual((dist / "SHA256SUMS").read_bytes(), b"previous sums")

    def test_packaging_cannot_skip_required_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing"
            for target, options in (
                ("linux-x64", {"run_smoke": False}),
                ("windows-x64", {"verify_headers": False}),
                ("android-arm64-v8a", {"verify_headers": False}),
            ):
                with (
                    self.subTest(target=target, options=options),
                    self.assertRaisesRegex(ValueError, "mandatory"),
                ):
                    package_target(target, missing, missing, **options)

    def test_packaging_rejects_a_foreign_copy_even_if_the_source_is_restored(
        self,
    ) -> None:
        lock = load_lock()
        profile = load_profiles()["linux-x64"]
        dep_ids: dict[str, str] = {}
        for name in resolve_build_order(profile.name, lock, profile):
            dep_ids[name] = dependency_identity(
                name,
                lock=lock,
                profile=profile,
                compiler_version="fixture compiler",
                upstream_ids=dep_ids,
            )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prefix = root / "prefix"
            (prefix / "lib").mkdir(parents=True)
            installed = prefix / "lib/libmpv.so.2"
            installed.write_bytes(b"compiled runtime")
            shutil.copytree(ROOT / "include/mpv", prefix / "include/mpv")
            (prefix / "build-info.json").write_text(
                json.dumps(
                    {
                        "release": lock.release,
                        "target": profile.name,
                        "compiler": "fixture compiler",
                        "profile": profile.to_dict(),
                        "depIdentities": dep_ids,
                        "identity": dep_ids["mpv"],
                        "deliveryInputs": _delivery_inputs_snapshot(profile, prefix),
                    }
                )
            )
            dist = root / "dist"
            dist.mkdir()
            (dist / "previous.zip").write_bytes(b"previous")
            real_copy = shutil.copyfile

            def copy(source: Path, destination: Path) -> Path:
                if source == installed:
                    original = installed.read_bytes()
                    installed.write_bytes(b"foreign runtime")
                    try:
                        return real_copy(source, destination)
                    finally:
                        installed.write_bytes(original)
                return real_copy(source, destination)

            with (
                mock.patch("libmpv_build.package.shutil.copyfile", side_effect=copy),
                mock.patch(
                    "libmpv_build.package.subprocess.run",
                ) as strip,
                mock.patch("libmpv_build.package.verify_library_file", return_value={}),
            ):
                with self.assertRaisesRegex(ValueError, "Copied runtime differs"):
                    package_target(profile.name, prefix, dist)
                strip.assert_not_called()
            self.assertEqual(installed.read_bytes(), b"compiled runtime")
            self.assertEqual(
                {p.name: p.read_bytes() for p in dist.iterdir()},
                {"previous.zip": b"previous"},
            )

    def test_build_info_rejects_changed_dependency_with_same_release(self) -> None:
        lock = load_lock()
        profile = load_profiles()["linux-x64"]
        dep_ids: dict[str, str] = {}
        for name in resolve_build_order(profile.name, lock, profile):
            dep_ids[name] = dependency_identity(
                name,
                lock=lock,
                profile=profile,
                compiler_version="fixture compiler",
                upstream_ids=dep_ids,
            )
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td)
            (prefix / "lib").mkdir()
            (prefix / "lib/libmpv.so.2").write_bytes(b"compiled runtime")
            (prefix / "include/mpv").mkdir(parents=True)
            (prefix / "include/mpv/client.h").write_bytes(b"compiled public header")
            (prefix / "build-info.json").write_text(
                json.dumps(
                    {
                        "release": lock.release,
                        "target": profile.name,
                        "compiler": "fixture compiler",
                        "profile": profile.to_dict(),
                        "depIdentities": dep_ids,
                        "identity": dep_ids["mpv"],
                        "deliveryInputs": _delivery_inputs_snapshot(profile, prefix),
                    }
                ),
                encoding="utf-8",
            )
            _validate_build_info(lock, profile, prefix)
            for path in (prefix / "lib/libmpv.so.2", prefix / "include/mpv/client.h"):
                with self.subTest(replaced=path.relative_to(prefix)):
                    original = path.read_bytes()
                    path.write_bytes(b"valid but foreign build output")
                    with self.assertRaisesRegex(ValueError, "delivery inputs differ"):
                        _validate_build_info(lock, profile, prefix)
                    path.write_bytes(original)
            metadata = json.loads((prefix / "build-info.json").read_text())
            metadata.pop("deliveryInputs")
            (prefix / "build-info.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "delivery inputs differ"):
                _validate_build_info(lock, profile, prefix)
            changed = copy.deepcopy(lock.raw)
            changed["dependencies"]["ffmpeg"]["sha256"] = "0" * 64
            changed_lock = prefix / "changed-lock.json"
            changed_lock.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stale build identity"):
                _validate_build_info(load_lock(changed_lock), profile, prefix, ROOT)

    def test_cleanup_rejects_unknown_target_without_removing_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td)
            artifact = dist / "keep.zip"
            artifact.write_bytes(b"previous")
            with self.assertRaises(ValueError):
                clean_dist(dist, "0.42.0-r6", ["linux-typo"])
            self.assertEqual(artifact.read_bytes(), b"previous")


if __name__ == "__main__":
    unittest.main()
