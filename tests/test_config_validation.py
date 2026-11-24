from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from libmpv_build import ROOT
from libmpv_build.config import load_lock, load_profiles, validate_https_url


class ConfigValidationTests(unittest.TestCase):
    def test_profiles_reject_mismatched_abi_unsafe_library_and_invalid_options(
        self,
    ) -> None:
        original = json.loads((ROOT / "targets.json").read_text(encoding="utf-8"))
        cases = (
            ("android-arm64-v8a", "system", "linux"),
            ("android-arm64-v8a", "cpu", "x86_64"),
            ("android-arm64-v8a", "triple", "../../host-compiler"),
            ("linux-x64", "library", "../../outside.so"),
            ("windows-x64", "library", "libmpv.so.2"),
            ("android-arm64-v8a", "api", True),
            ("android-arm64-v8a", "api", "28"),
            ("android-arm64-v8a", "api", 19),
            ("linux-x64", "api", 28),
            ("linux-x64", "ffmpeg", "--disable-debug"),
            ("linux-x64", "mpv", ["x11=enabled\nmalformed"]),
        )
        for target, field, value in cases:
            with (
                self.subTest(target=target, field=field, value=value),
                tempfile.TemporaryDirectory() as td,
            ):
                candidate = copy.deepcopy(original)
                candidate[target][field] = value
                path = Path(td) / "targets.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_profiles(path)

    def test_lock_rejects_unlocked_toolchains_and_invalid_required_fields(self) -> None:
        original = load_lock().raw
        cases = (
            ("toolchains", "android", "sha256", "not-a-digest"),
            ("toolchains", "windows", "url", "https:///missing-host"),
            ("toolchains", "android", "revision", "../ndk"),
            ("toolchains", "linux", "image", "ubuntu:22.04"),
            ("toolchains", "linux", "glibc", "2.36"),
            ("buildTools", "meson", "sha256", None),
            ("dependencies", "mpv", "commit", None),
            ("dependencies", "mpv", "date", "not-a-date"),
            ("dependencies", "mpv", "targets", "linux-x64"),
            ("dependencies", "mpv", "targets", ["linux-x64", "linux-x64"]),
        )
        for section, name, field, value in cases:
            with (
                self.subTest(section=section, name=name, field=field, value=value),
                tempfile.TemporaryDirectory() as td,
            ):
                candidate = copy.deepcopy(original)
                candidate[section][name][field] = value
                path = Path(td) / "dependencies.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_lock(path)

    def test_lock_rejects_submodule_traversal_unknown_parents_overlaps_and_cycles(
        self,
    ) -> None:
        original = load_lock().raw
        for invalid in (
            "../outside",
            "/outside",
            "dir/../../outside",
            "dir\\outside",
            ".complete.json",
        ):
            with self.subTest(path=invalid), tempfile.TemporaryDirectory() as td:
                candidate = copy.deepcopy(original)
                candidate["dependencies"]["freetype_dlg"]["pinFrom"]["path"] = invalid
                path = Path(td) / "lock.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_lock(path)

        for case in ("missing_parent", "cycle", "overlap"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                candidate = copy.deepcopy(original)
                if case == "missing_parent":
                    candidate["dependencies"]["freetype_dlg"]["pinFrom"]["parent"] = (
                        "missing"
                    )
                elif case == "cycle":
                    candidate["dependencies"]["freetype"]["pinFrom"] = {
                        "type": "gitlink",
                        "parent": "freetype_dlg",
                        "path": "recursive",
                    }
                else:
                    candidate["dependencies"]["libplacebo_jinja"]["pinFrom"]["path"] = (
                        "3rdparty/glad/nested"
                    )
                path = Path(td) / "lock.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_lock(path)

    def test_invalid_config_shapes_fail_with_value_error(self) -> None:
        for raw in ([], None, {}, {"release": None}):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "input.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_lock(path)
                with self.assertRaises(ValueError):
                    load_profiles(path)

    def test_https_validation_requires_an_actual_host_and_rejects_control_characters(
        self,
    ) -> None:
        for url in (
            "https:///missing",
            "https://",
            "https://host/with\x00null",
            "https://host/with space",
            "http://host/file",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_https_url(url, "test")
        validate_https_url("https://host.example/archive?revision=custom", "test")

    def test_valid_custom_versions_and_profile_options_remain_supported(self) -> None:
        original = copy.deepcopy(load_lock().raw)
        original["dependencies"]["mpv"]["version"] = "0.42.0-custom"
        original["toolchains"]["windows"]["version"] = "custom-build"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lock.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(
                load_lock(path).dependencies["mpv"].version, "0.42.0-custom"
            )
            profiles = json.loads((ROOT / "targets.json").read_text(encoding="utf-8"))
            profiles["linux-x64"]["ffmpeg"].append("--extra-cflags=-O2 -fPIC")
            path.write_text(json.dumps(profiles), encoding="utf-8")
            self.assertIn(
                "--extra-cflags=-O2 -fPIC", load_profiles(path)["linux-x64"].ffmpeg
            )


if __name__ == "__main__":
    unittest.main()
