from __future__ import annotations

import copy
import inspect
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from libmpv_build import ROOT
from libmpv_build.config import TARGETS, load_lock, load_profiles
from libmpv_build.recipes import (
    RECIPES,
    _ffmpeg_flags,
    _validate_ffmpeg_components,
    dependency_identity,
    resolve_build_order,
)
from libmpv_build.runners import run_meson
from libmpv_build.toolchains import ToolchainContext


class ConfigAndRecipesTests(unittest.TestCase):
    def test_lock_and_profiles_valid(self) -> None:
        lock = load_lock(ROOT / "dependencies.json")
        profiles = load_profiles(ROOT / "targets.json")
        self.assertNotIn("schemaVersion", lock.raw)
        self.assertEqual(lock.release, "0.42.0-r7")
        self.assertEqual(set(profiles), set(TARGETS))
        self.assertNotIn("zlib", lock.dependencies)
        self.assertIn("vulkan-headers", lock.dependencies)
        self.assertIn("zlib-ng", lock.dependencies)
        self.assertIn("uchardet", lock.dependencies)
        self.assertNotIn("windows-x64", lock.dependencies["fontconfig"].targets)
        for target in TARGETS:
            self.assertIn(target, lock.dependencies["libxml2"].targets)
        self.assertEqual(
            set(lock.dependencies["libiconv"].targets),
            {"android-arm64-v8a", "android-x86_64"},
        )
        self.assertEqual(lock.dependencies["win-iconv"].targets, ("windows-x64",))
        self.assertEqual(
            set(lock.dependencies["nv-codec-headers"].targets),
            {"windows-x64", "linux-x64"},
        )
        self.assertIn("--enable-directwrite", profiles["windows-x64"].libass)
        self.assertIn("--disable-fontconfig", profiles["windows-x64"].libass)

    def test_uchardet_uses_supported_x64_simd_instead_of_float_store_fallback(self) -> None:
        options = RECIPES["uchardet"].signature_for(
            load_profiles()["windows-x64"], load_lock()
        )["options"]
        self.assertIn("-DCHECK_SSE2=ON", options)
        self.assertNotIn("-DCHECK_SSE2=OFF", options)

    def test_topological_order_per_target(self) -> None:
        lock = load_lock(ROOT / "dependencies.json")
        profiles = load_profiles(ROOT / "targets.json")
        for target in TARGETS:
            order = resolve_build_order(target, lock, profiles[target])
            self.assertEqual(order[-1], "mpv")
            self.assertIn("zlib-ng", order)
            self.assertIn("libxml2", order)
            self.assertIn("uchardet", order)
            self.assertIn("vulkan-headers", order)
            self.assertLess(order.index("vulkan-headers"), order.index("libplacebo"))
            if target == "windows-x64":
                self.assertNotIn("fontconfig", order)
                self.assertIn("win-iconv", order)
                self.assertLess(order.index("win-iconv"), order.index("ffmpeg"))
                self.assertNotIn("libiconv", order)
                self.assertIn("nv-codec-headers", order)
                self.assertIn("shaderc", order)
                self.assertIn("spirv-cross", order)
            elif target == "linux-x64":
                self.assertIn("fontconfig", order)
                self.assertIn("nv-codec-headers", order)
                self.assertIn("wayland", order)
                self.assertIn("wayland-protocols", order)
                self.assertIn("wayland=enabled", profiles[target].mpv)
                self.assertIn("dmabuf-wayland=enabled", profiles[target].mpv)
                self.assertNotIn("win-iconv", order)
                self.assertNotIn("libiconv", order)
                self.assertNotIn("shaderc", order)
            else:
                self.assertIn("fontconfig", order)
                self.assertIn("libiconv", order)
                self.assertLess(order.index("libiconv"), order.index("ffmpeg"))
                self.assertNotIn("win-iconv", order)
                self.assertNotIn("nv-codec-headers", order)
                self.assertNotIn("wayland", order)
                self.assertNotIn("wayland-protocols", order)
                self.assertNotIn("shaderc", order)

    def test_dependency_identity_isolation(self) -> None:
        lock = load_lock(ROOT / "dependencies.json")
        profiles = load_profiles(ROOT / "targets.json")
        profile = profiles["linux-x64"]
        order = resolve_build_order("linux-x64", lock, profile)

        def compute_all(cur_lock) -> dict[str, str]:
            ids: dict[str, str] = {}
            for name in order:
                ids[name] = dependency_identity(
                    name,
                    lock=cur_lock,
                    profile=profile,
                    compiler_version="clang-14.0",
                    upstream_ids=ids,
                )
            return ids

        base_ids = compute_all(lock)

        # Mutate mpv commit: only mpv should invalidate
        raw_mpv = copy.deepcopy(lock.raw)
        raw_mpv["dependencies"]["mpv"]["commit"] = "a" * 40
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dependencies.json"
            p.write_text(json.dumps(raw_mpv), encoding="utf-8")
            mpv_ids = compute_all(load_lock(p))
        for name in order:
            if name == "mpv":
                self.assertNotEqual(mpv_ids[name], base_ids[name])
            else:
                self.assertEqual(mpv_ids[name], base_ids[name])

        # Mutate zlib-ng commit: zlib-ng and its dependents invalidate, while independent deps stay cached
        raw_zlib = copy.deepcopy(lock.raw)
        raw_zlib["dependencies"]["zlib-ng"]["commit"] = "b" * 40
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dependencies.json"
            p.write_text(json.dumps(raw_zlib), encoding="utf-8")
            zlib_ids = compute_all(load_lock(p))
        for independent in ("dav1d", "fribidi", "libunibreak", "uchardet"):
            self.assertEqual(zlib_ids[independent], base_ids[independent])
        for dependent in ("zlib-ng", "freetype", "ffmpeg", "mpv"):
            self.assertNotEqual(zlib_ids[dependent], base_ids[dependent])

    def test_identity_tracks_target_abi_and_actual_recipe_implementation(self) -> None:
        lock = load_lock()
        profile = load_profiles()["android-arm64-v8a"]

        def identity(cur_profile, name="zlib-ng", upstream=None):
            return dependency_identity(
                name,
                lock=lock,
                profile=cur_profile,
                compiler_version="clang",
                upstream_ids=upstream or {},
            )

        original = identity(profile)
        for changes in (
            {"api": 29},
            {"cpu": "x86_64"},
            {"triple": "x86_64-linux-android"},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual(original, identity(replace(profile, **changes)))

        original_source = inspect.getsource

        def changed_source(function):
            return original_source(function) + (
                "\nchanged build flags" if function is RECIPES["mpv"].build else ""
            )

        upstream = {name: "stable" for name in RECIPES["mpv"].deps_for(profile.system)}
        mpv_identity = identity(profile, "mpv", upstream)
        with mock.patch(
            "libmpv_build.recipes.inspect.getsource", side_effect=changed_source
        ):
            self.assertEqual(original, identity(profile))
            self.assertNotEqual(mpv_identity, identity(profile, "mpv", upstream))

    def test_identity_tracks_shared_runner_changes(self) -> None:
        lock = load_lock()
        profile = load_profiles()["linux-x64"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(ROOT / "libmpv_build", root / "libmpv_build")
            shutil.copy2(ROOT / "build-config.json", root / "build-config.json")
            shutil.copy2(ROOT / "Dockerfile", root / "Dockerfile")

            def identity():
                return dependency_identity(
                    "zlib-ng",
                    lock=lock,
                    profile=profile,
                    compiler_version="clang",
                    upstream_ids={},
                    root=root,
                )

            original = identity()
            runner = root / "libmpv_build/runners.py"
            runner.write_text(
                runner.read_text(encoding="utf-8") + "\n# Updated compiler flags\n",
                encoding="utf-8",
            )
            changed = identity()
            self.assertNotEqual(original, changed)
            cfg = root / "build-config.json"
            cfg_data = json.loads(cfg.read_text(encoding="utf-8"))
            cfg_data["optimizationTiers"]["zlib-ng"] = "O2"
            cfg.write_text(json.dumps(cfg_data), encoding="utf-8")
            self.assertNotEqual(changed, identity())

    def test_ffmpeg_disables_debug_and_uses_o2_without_size_mode(self) -> None:
        profiles = load_profiles()
        for target, expect_hidden in (
            ("linux-x64", True),
            ("android-arm64-v8a", False),
        ):
            with self.subTest(target=target):
                profile = profiles[target]
                tc = ToolchainContext(
                    profile=profile,
                    toolroot=None,
                    prefix=Path("/prefix"),
                    cc="clang",
                    cxx="clang++",
                    ar="ar",
                    ranlib="ranlib",
                    nm="nm",
                    strip="strip",
                    objcopy="objcopy",
                    windres=None,
                    compiler_version="clang",
                    cflags=["-Oz", "-flto=full", "-fvisibility=hidden"],
                    cxxflags=["-Oz", "-flto=full", "-fvisibility=hidden"],
                    ldflags=[],
                    env={"CFLAGS": "-Oz -fvisibility=hidden", "LDFLAGS": ""},
                )
                flags, env = _ffmpeg_flags(tc)
                self.assertIn("--disable-debug", flags)
                self.assertIn("--optflags=-O2", flags)
                self.assertIn("--enable-lto=full", flags)
                self.assertIn("--enable-libxml2", flags)
                self.assertNotIn("--disable-postproc", flags)
                self.assertIn("--enable-iconv", flags)
                if target.startswith("android"):
                    self.assertIn("--extra-libs=-liconv", flags)
                self.assertIn("--disable-decoders", flags)
                self.assertIn("--disable-demuxers", flags)
                self.assertIn("--disable-parsers", flags)
                self.assertIn("--disable-bsfs", flags)
                self.assertTrue(any(f.startswith("--enable-decoder=") for f in flags))
                self.assertTrue(any(f.startswith("--enable-demuxer=") for f in flags))
                self.assertTrue(any(f.startswith("--enable-parser=") for f in flags))
                self.assertTrue(any(f.startswith("--enable-bsf=") for f in flags))
                self.assertNotIn("--enable-small", flags)
                self.assertIn("-O2", env["CFLAGS"])
                self.assertNotIn("-Oz", env["CFLAGS"])
                if expect_hidden:
                    self.assertIn("-fvisibility=hidden", env["CFLAGS"])
                else:
                    self.assertNotIn("-fvisibility=hidden", env["CFLAGS"])

    def test_wayland_native_setup_uses_the_target_toolchain_without_cross_build(
        self,
    ) -> None:
        tc = mock.Mock()
        tc.cflags_for.return_value = ["-Oz", "-flto=full"]
        tc.cxxflags_for.return_value = ["-Oz", "-flto=full"]
        tc.env = {}
        tc.prefix = Path("/prefix")
        with (
            mock.patch("libmpv_build.runners.require_executable", return_value="meson"),
            mock.patch("libmpv_build.runners.run_cmd") as run,
        ):
            run_meson(
                tc,
                Path("/base"),
                Path("/source"),
                Path("/build"),
                Path("/dest"),
                2,
                native=True,
            )
        setup = run.call_args_list[0].args[0]
        self.assertNotIn("--cross-file", setup)
        self.assertEqual(setup.count("--native-file"), 1)
        self.assertIn(Path("/base/cross.ini"), setup)
        self.assertNotIn(Path("/base/native.ini"), setup)
        self.assertIn("-Dc_args=['-Oz', '-flto=full']", setup)

    def test_ffmpeg_rejects_silent_component_disabling_and_unknown_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            build_dir = Path(td)
            with (
                mock.patch(
                    "libmpv_build.recipes._ffmpeg_decoders", return_value=("h264",)
                ),
                mock.patch("libmpv_build.recipes.FFMPEG_DEMUXERS", ("image_png_pipe",)),
                mock.patch("libmpv_build.recipes.FFMPEG_PARSERS", ()),
                mock.patch("libmpv_build.recipes.FFMPEG_BSFS", ()),
                mock.patch("libmpv_build.recipes.FFMPEG_PROTOCOLS", ()),
                mock.patch(
                    "libmpv_build.recipes.FFMPEG_FILTERS",
                    ("buffer", "buffersink", "null"),
                ),
            ):
                header = "#define CONFIG_H264_DECODER 1\n#define CONFIG_NULL_FILTER 1\n"
                path = build_dir / "config_components.h"
                for image_macro in ("", "#define CONFIG_IMAGE_PNG_PIPE_DEMUXER 0\n"):
                    with self.subTest(image_macro=image_macro):
                        path.write_text(header + image_macro, encoding="utf-8")
                        with self.assertRaisesRegex(
                            RuntimeError, "image_png_pipe_demuxer"
                        ):
                            _validate_ffmpeg_components(build_dir, "linux")
                path.write_text(
                    header + "#define CONFIG_IMAGE_PNG_PIPE_DEMUXER 1\n",
                    encoding="utf-8",
                )
                _validate_ffmpeg_components(build_dir, "linux")


if __name__ == "__main__":
    unittest.main()
