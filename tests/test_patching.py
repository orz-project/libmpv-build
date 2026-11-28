from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from libmpv_build.config import LINUX_SYSTEM_LIBRARIES, STATIC_PACKAGES
from libmpv_build.patching import (
    PatchError,
    enforce_static_meson_dependencies,
    patch_freetype_modules,
    patch_mpv_meson,
    patch_mpv_wayland_compat,
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

    def test_mpv_static_iconv_probes_the_installed_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)
            meson = source / "meson.build"
            original = "iconv = dependency('iconv', required: get_option('iconv'))\n"
            meson.write_text(original, encoding="utf-8")
            enforce_static_meson_dependencies(source, STATIC_PACKAGES)
            patch_mpv_meson(
                source,
                final_link_args=[],
                enable_cpp_link=False,
                static_iconv_dir=Path("/prefix/lib"),
            )
            self.assertEqual(
                meson.read_text(encoding="utf-8"),
                "iconv = cc.find_library('iconv', dirs: ['/prefix/lib'], "
                "static: true, required: get_option('iconv'))\n",
            )
            with self.assertRaises(PatchError):
                patch_mpv_meson(
                    source,
                    final_link_args=[],
                    enable_cpp_link=False,
                    static_iconv_dir=Path("/prefix/lib"),
                )

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

    def test_patch_freetype_modules_disables_legacy_and_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)
            cfg = src / "modules.cfg"
            lines = [
                "FONT_MODULES += truetype",
                "FONT_MODULES += type1",
                "FONT_MODULES += cff",
                "FONT_MODULES += cid",
                "FONT_MODULES += hvf",
                "FONT_MODULES += pfr",
                "FONT_MODULES += type42",
                "FONT_MODULES += winfonts",
                "FONT_MODULES += pcf",
                "FONT_MODULES += bdf",
                "FONT_MODULES += sfnt",
                "RASTER_MODULES += smooth",
                "RASTER_MODULES += raster",
                "RASTER_MODULES += svg",
                "RASTER_MODULES += sdf",
                "AUX_MODULES += lzw",
                "AUX_MODULES += bzip2",
                "BASE_EXTENSIONS += ftbdf.c",
                "BASE_EXTENSIONS += ftcid.c",
                "BASE_EXTENSIONS += ftpfr.c",
                "BASE_EXTENSIONS += fttype1.c",
                "BASE_EXTENSIONS += ftwinfnt.c",
            ]
            cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
            patch_freetype_modules(src)
            patched = cfg.read_text(encoding="utf-8")
            self.assertIn("FONT_MODULES += truetype\n", patched)
            self.assertIn("# FONT_MODULES += type1\n", patched)
            self.assertIn("\nBASE_EXTENSIONS += fttype1.c\n", patched)
            self.assertNotIn("# BASE_EXTENSIONS += fttype1.c", patched)
            self.assertIn("# BASE_EXTENSIONS += ftwinfnt.c\n", patched)
            with self.assertRaises(PatchError):
                patch_freetype_modules(src)

    def test_patch_mpv_wayland_compat_preserves_system_120_abi_and_fails_fast(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)
            wayland_common = src / "video/out/wayland_common.c"
            wayland_common.parent.mkdir(parents=True)
            wayland_common.write_text(
                "#ifdef WL_FIXES_ACK_GLOBAL_REMOVE\n"
                "    if (!strcmp(interface, wl_fixes_interface.name) && (ver >= 2) && found++) {\n"
                "        wl->fixes = wl_registry_bind(reg, id, &wl_fixes_interface, ver);\n"
                "    }\n"
                "#endif\n"
                "#ifdef WL_FIXES_ACK_GLOBAL_REMOVE\n"
                "    if (wl->fixes)\n"
                "        wl_fixes_ack_global_remove(wl->fixes, reg, id);\n"
                "#endif\n"
                "#ifdef WL_FIXES_DESTROY\n"
                "    if (wl->fixes)\n"
                "        wl_fixes_destroy(wl->fixes);\n"
                "#endif\n"
                'q1 = wl_display_create_queue_with_name(wl->display, "image description info queue");\n'
                'q2 = wl_display_create_queue_with_name(wl->display, "image description creator queue");\n',
                encoding="utf-8",
            )
            patch_mpv_wayland_compat(src)
            patched = wayland_common.read_text(encoding="utf-8")
            self.assertNotIn("wl_display_create_queue_with_name", patched)
            self.assertEqual(patched.count("wl_display_create_queue(wl->display)"), 2)
            self.assertNotIn("#ifdef WL_FIXES_ACK_GLOBAL_REMOVE", patched)
            self.assertNotIn("#ifdef WL_FIXES_DESTROY", patched)
            self.assertEqual(
                patched.count(
                    "#if 0 /* System libwayland 1.20 lacks the optional wl_fixes_interface. */"
                ),
                3,
            )
            with self.assertRaises(PatchError):
                patch_mpv_wayland_compat(src)


if __name__ == "__main__":
    unittest.main()
