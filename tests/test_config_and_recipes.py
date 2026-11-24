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
    dependency_identity,
    resolve_build_order,
)
from libmpv_build.toolchains import ToolchainContext


class ConfigAndRecipesTests(unittest.TestCase):
    def test_lock_and_profiles_valid(self) -> None:
        lock = load_lock(ROOT / "dependencies.json")
        profiles = load_profiles(ROOT / "targets.json")
        self.assertNotIn("schemaVersion", lock.raw)
        self.assertEqual(lock.release, "0.42.0-r6")
        self.assertEqual(set(profiles), set(TARGETS))
        self.assertNotIn("windows-x64", lock.dependencies["fontconfig"].targets)
        self.assertNotIn("windows-x64", lock.dependencies["libxml2"].targets)
        self.assertIn("--enable-directwrite", profiles["windows-x64"].libass)
        self.assertIn("--disable-fontconfig", profiles["windows-x64"].libass)

    def test_topological_order_per_target(self) -> None:
        lock = load_lock(ROOT / "dependencies.json")
        profiles = load_profiles(ROOT / "targets.json")
        for target in TARGETS:
            order = resolve_build_order(target, lock, profiles[target])
            self.assertEqual(order[-1], "mpv")
            if target == "windows-x64":
                self.assertNotIn("fontconfig", order)
                self.assertNotIn("libxml2", order)
                self.assertIn("shaderc", order)
                self.assertIn("spirv-cross", order)
            else:
                self.assertIn("fontconfig", order)
                self.assertIn("libxml2", order)
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

        # Mutate zlib commit: zlib and its dependents invalidate, but dav1d/fribidi/libunibreak/vulkan-headers stay cached
        raw_zlib = copy.deepcopy(lock.raw)
        raw_zlib["dependencies"]["zlib"]["commit"] = "b" * 40
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dependencies.json"
            p.write_text(json.dumps(raw_zlib), encoding="utf-8")
            zlib_ids = compute_all(load_lock(p))
        for independent in ("dav1d", "fribidi", "libunibreak", "vulkan-headers"):
            self.assertEqual(zlib_ids[independent], base_ids[independent])
        for dependent in ("zlib", "freetype", "ffmpeg", "mpv"):
            self.assertNotEqual(zlib_ids[dependent], base_ids[dependent])

    def test_identity_tracks_target_abi_and_actual_recipe_implementation(self) -> None:
        lock = load_lock()
        profile = load_profiles()["android-arm64-v8a"]

        def identity(cur_profile, name="zlib", upstream=None):
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
            shutil.copy2(ROOT / "Dockerfile", root / "Dockerfile")

            def identity():
                return dependency_identity(
                    "zlib",
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
            self.assertNotEqual(original, identity())

    def test_ffmpeg_disables_debug_and_uses_o2_without_size_mode(self) -> None:
        profile = load_profiles()["linux-x64"]
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
            cflags=["-Os", "-flto=thin"],
            cxxflags=[],
            ldflags=[],
            env={"CFLAGS": "-Os", "LDFLAGS": ""},
        )
        flags, env = _ffmpeg_flags(tc)
        self.assertIn("--disable-debug", flags)
        self.assertIn("--optflags=-O2", flags)
        self.assertNotIn("--enable-small", flags)
        self.assertIn("-O2", env["CFLAGS"])
        self.assertNotIn("-Os", env["CFLAGS"])


if __name__ == "__main__":
    unittest.main()
