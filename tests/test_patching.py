from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from libmpv_build.config import LINUX_SYSTEM_LIBRARIES, STATIC_PACKAGES
from libmpv_build.patching import (
    PatchError,
    enforce_static_meson_dependencies,
    patch_mpv_meson,
    replace_exact,
    rewrite_linux_pc_system_libs,
)


class PatchingTests(unittest.TestCase):
    def test_replace_exact_fails_fast_on_missing_or_multiple(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "file.txt"
            target.write_text("alpha beta alpha", encoding="utf-8")
            with self.assertRaises(PatchError):
                replace_exact(target, "missing", "x", expected_count=1)
            with self.assertRaises(PatchError):
                replace_exact(target, "alpha", "x", expected_count=1)
            replace_exact(target, "beta", "gamma", expected_count=1)
            self.assertEqual(target.read_text(encoding="utf-8"), "alpha gamma alpha")

    def test_static_meson_dependencies_and_mpv_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)
            meson = src / "meson.build"
            meson.write_text(
                "d1 = dependency('libavcodec', required: true)\n"
                "d2 = dependency('zlib', static: true)\n"
                "d3 = dependency('egl')\n"
                "libmpv = library('mpv', sources,\n"
                "  link_args: cc.get_supported_link_arguments(['-Wl,-Bsymbolic']),\n"
                ")\n",
                encoding="utf-8",
            )
            changed = enforce_static_meson_dependencies(src, STATIC_PACKAGES)
            self.assertEqual(changed, 1)
            text = meson.read_text(encoding="utf-8")
            self.assertIn(
                "dependency('libavcodec', static: true, required: true)", text
            )
            self.assertEqual(text.count("static: true"), 2)
            self.assertIn("dependency('egl')", text)

            patch_mpv_meson(
                src, final_link_args=["-Wl,--as-needed"], enable_cpp_link=True
            )
            patched = meson.read_text(encoding="utf-8")
            self.assertIn("add_languages('cpp', native: false)", patched)
            self.assertIn("link_language: 'cpp'", patched)
            self.assertIn("['-Wl,--as-needed']", patched)

    def test_static_meson_patch_ignores_comments_strings_and_nested_keyword_arguments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)
            meson = source / "meson.build"
            meson.write_text(
                "# dependency('libass', static: false)\n"
                "message(\"dependency('libass', static: false)\")\n"
                "dep = dependency ('libass',\n"
                "  # static: false is not a keyword argument\n"
                "  required: some_function(static: false),\n"
                ")\n",
                encoding="utf-8",
            )
            self.assertEqual(
                enforce_static_meson_dependencies(source, STATIC_PACKAGES), 1
            )
            patched = meson.read_text(encoding="utf-8")
            self.assertEqual(patched.count("static: true"), 1)
            self.assertIn("dependency ('libass', static: true,", patched)
            self.assertIn("# dependency('libass', static: false)", patched)
            self.assertIn("message(\"dependency('libass', static: false)\")", patched)

    def test_static_meson_patch_rejects_conflicting_or_unterminated_dependency(
        self,
    ) -> None:
        cases = (
            "dep = dependency('libass', static: false)\n",
            "dep = dependency('libass', static: get_option('prefer_static'))\n",
            "dep = dependency('libass', required: true\n",
        )
        for original in cases:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as td:
                source = Path(td)
                meson = source / "meson.build"
                meson.write_text(original, encoding="utf-8")
                with self.assertRaises(PatchError):
                    enforce_static_meson_dependencies(source, STATIC_PACKAGES)
                self.assertEqual(meson.read_text(encoding="utf-8"), original)

    def test_rewrite_linux_pc_system_libs_leaves_unmatched_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pc_dir = Path(td)
            pc = pc_dir / "sample.pc"
            pc.write_text("Libs: -L/tmp/lib -lcustom -lm\n", encoding="utf-8")
            if Path("/usr/lib/x86_64-linux-gnu/libm.so").is_file():
                rewrite_linux_pc_system_libs(pc_dir, LINUX_SYSTEM_LIBRARIES)
                self.assertIn(
                    "/usr/lib/x86_64-linux-gnu/libm.so", pc.read_text(encoding="utf-8")
                )
                self.assertIn("-lcustom", pc.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
