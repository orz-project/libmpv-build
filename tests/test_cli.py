from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from libmpv_build.cli import main
from libmpv_build.locks import file_lock


class CliPackageTests(unittest.TestCase):
    def test_container_image_command_builds_the_locked_local_image(self) -> None:
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            mock.patch("libmpv_build.cli.build_container_image", return_value="libmpv-build:test"),
            mock.patch("libmpv_build.cli.ROOT", Path("/tmp/libmpv-build-test-root")),
            mock.patch("libmpv_build.cli.load_lock") as load_lock,
        ):
            self.assertEqual(main(["container-image"]), 0)
        load_lock.assert_called_once()
        self.assertIn("libmpv-build:test", output.getvalue())

    def test_image_reuse_option_requires_container_mode(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch("libmpv_build.cli.build_targets") as build,
            self.assertRaises(SystemExit) as failure,
        ):
            main(
                [
                    "build",
                    "--target",
                    "linux-x64",
                    "--reuse-container-image",
                ]
            )
        self.assertEqual(failure.exception.code, 2)
        build.assert_not_called()

    def test_package_cannot_skip_the_mandatory_linux_smoke_gate(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch("libmpv_build.cli.package_target") as package,
            self.assertRaises(SystemExit) as failure,
        ):
            main(
                [
                    "package",
                    "--target",
                    "linux-x64",
                    "--prefix",
                    "unused",
                    "--skip-smoke",
                ]
            )
        self.assertEqual(failure.exception.code, 2)
        package.assert_not_called()

class LockSafetyTests(unittest.TestCase):
    def test_symlinked_lock_cannot_truncate_another_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            protected = root / "dependencies.json"
            protected.write_bytes(b"existing dependency lock")
            path = root / "update.lock"
            path.symlink_to(protected)
            with self.assertRaises(OSError), file_lock(path):
                self.fail("Symbolic link was opened as a lock")
            self.assertEqual(protected.read_bytes(), b"existing dependency lock")


if __name__ == "__main__":
    unittest.main()
