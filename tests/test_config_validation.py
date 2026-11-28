from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from libmpv_build import ROOT
from libmpv_build.config import (
    load_build_config,
    load_lock,
    load_profiles,
    validate_https_url,
)


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
            ("android-arm64-v8a", "api", 27),
            ("android-arm64-v8a", "api", 29),
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
            ("buildTools", "ninja", "sha256", None),
            ("buildTools", "cmake", "sha256", "not-a-digest"),
            ("toolchains", "linux", "meson", "1.0.0"),
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

        for missing_tool in ("meson", "ninja", "cmake"):
            with self.subTest(missing_tool=missing_tool), tempfile.TemporaryDirectory() as td:
                candidate = copy.deepcopy(original)
                candidate["buildTools"].pop(missing_tool)
                path = Path(td) / "dependencies.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, missing_tool):
                    load_lock(path)

    def test_lock_rejects_git_pins_with_a_tarball_parent(self) -> None:
        candidate = copy.deepcopy(load_lock().raw)
        candidate["dependencies"]["freetype_dlg"]["pinFrom"]["parent"] = "libiconv"
        candidate["dependencies"]["freetype_dlg"]["targets"] = ["android-arm64-v8a"]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lock.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "parent must be Git-locked"):
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

    def test_tarball_dependency_validation(self) -> None:
        original = copy.deepcopy(load_lock().raw)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lock.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            self.assertIsNone(load_lock(path).dependencies["libiconv"].commit)
            self.assertIsNotNone(load_lock(path).dependencies["libiconv"].url)

            bad_cases = (
                {"url": "http://ftp.gnu.org/libiconv.tar.gz"},
                {"repository": "https://example.com/libiconv.git"},
                {"commit": "a" * 40},
                {"stripPrefix": None},
                {"stripPrefix": "../escape"},
                {"stripPrefix": "a/b"},
            )
            for mutation in bad_cases:
                with self.subTest(mutation=mutation):
                    candidate = copy.deepcopy(original)
                    if mutation == {"stripPrefix": None}:
                        candidate["dependencies"]["libiconv"].pop("stripPrefix")
                    else:
                        candidate["dependencies"]["libiconv"].update(mutation)
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_lock(path)

    def test_lock_loads_without_top_level_repository(self) -> None:
        lock = load_lock()
        self.assertNotIn("repository", lock.raw)
        self.assertEqual(
            lock.dependencies["mpv"].repository,
            "https://github.com/mpv-player/mpv.git",
        )

    def test_build_config_validation(self) -> None:
        cfg = load_build_config(ROOT / "build-config.json")
        self.assertEqual(cfg.toolchain.lto, "full")
        self.assertEqual(cfg.toolchain.meson_options, ("debug=false", "b_ndebug=true"))
        self.assertIn("-DNDEBUG", cfg.toolchain.common_cflags)
        self.assertIn("-fno-math-errno", cfg.toolchain.common_cflags)
        self.assertNotIn("-fmerge-all-constants", cfg.toolchain.common_cflags)
        self.assertNotIn("-fPIC", cfg.toolchain.common_cflags)
        self.assertTrue(
            any(flag.startswith("-Wl,--lto-O") for flag in cfg.toolchain.common_ldflags)
        )
        self.assertIn("-fPIC", cfg.toolchain.systems["linux"].cflags)
        self.assertIn("-Wl,-z,relro", cfg.toolchain.systems["linux"].ldflags)
        self.assertIn("-Wl,-z,now", cfg.toolchain.systems["linux"].ldflags)
        self.assertNotIn("-fPIC", cfg.toolchain.systems["windows"].cflags)
        self.assertEqual(cfg.verify.max_glibc_version, (2, 35))

        def mutate(fn):
            data = copy.deepcopy(cfg.raw)
            fn(data)
            return data

        bad_configs = (
            mutate(lambda d: d["toolchain"].__setitem__("lto", "invalid")),
            mutate(lambda d: d["toolchain"].pop("mesonOptions")),
            mutate(lambda d: d["toolchain"].__setitem__("mesonOptions", "debug=false")),
            mutate(lambda d: d["toolchain"].__setitem__("mesonOptions", [])),
            mutate(lambda d: d["toolchain"].__setitem__("mesonOptions", ["debug"])),
            mutate(lambda d: d["toolchain"].__setitem__("mesonOptions", ["debug="])),
            mutate(
                lambda d: d["toolchain"].__setitem__("mesonOptions", ["debug=false\n"])
            ),
            mutate(lambda d: d["optimizationTiers"].__setitem__("dav1d", "O9")),
            mutate(lambda d: d["optimizationTiers"].pop("libass")),
            mutate(lambda d: d["recipes"].pop("mpv")),
            mutate(lambda d: d["recipes"].__setitem__("fontconfig", [])),
            mutate(lambda d: d["recipes"]["libplacebo"].pop("windows")),
            mutate(lambda d: d["recipes"].__setitem__("dav1d", {"options": []})),
            mutate(lambda d: d["recipes"].__setitem__("dav1d", [])),
            mutate(lambda d: d["recipes"].__setitem__("uchardet", [])),
            mutate(lambda d: d["recipes"]["uchardet"].pop("x86_64")),
            mutate(lambda d: d["toolchain"]["march"].pop("aarch64")),
            mutate(lambda d: d["toolchain"]["systems"].pop("linux")),
            mutate(lambda d: d["verify"].__setitem__("maxGlibcVersion", [2])),
            mutate(lambda d: d["verify"].__setitem__("maxGlibcVersion", [2, 36])),
            mutate(
                lambda d: d["update"]["shadercDeps"][0].__setitem__(
                    "repository", "http://example.com/repo.git"
                )
            ),
            mutate(
                lambda d: d["patches"]["mpvWaylandCompat"][0].__setitem__(
                    "file", "../outside.c"
                )
            ),
            mutate(
                lambda d: d["patches"]["mpvWaylandCompat"][0].__setitem__(
                    "old", "a\rb"
                )
            ),
            mutate(
                lambda d: d["patches"]["mpvWaylandCompat"][0].__setitem__(
                    "old", "   \n  "
                )
            ),
        )
        for candidate in bad_configs:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "build-config.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_build_config(path)

    def test_export_configuration_cannot_drop_runtime_apis(self) -> None:
        original = load_build_config().raw
        for omitted in original["verify"]["requiredMpvExports"]:
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as td:
                candidate = copy.deepcopy(original)
                candidate["verify"]["requiredMpvExports"].remove(omitted)
                path = Path(td) / "build-config.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "required runtime APIs"):
                    load_build_config(path)

    def test_export_configuration_can_require_additional_public_apis(self) -> None:
        candidate = copy.deepcopy(load_build_config().raw)
        candidate["verify"]["requiredMpvExports"].append("mpv_request_log_messages")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "build-config.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            self.assertIn(
                "mpv_request_log_messages",
                load_build_config(path).verify.required_mpv_exports,
            )


if __name__ == "__main__":
    unittest.main()
