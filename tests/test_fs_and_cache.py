from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest import mock

from libmpv_build.config import load_lock
from libmpv_build.fs import (
    archive_hash_tree,
    atomic_replace_dir,
    capture_tree_manifest,
    extract_archive_safe,
    verify_tree_manifest,
)
from libmpv_build.locks import cache_lock
from libmpv_build.process import run_retry
from libmpv_build.sources import (
    ensure_source_tree,
    fetch_commit,
    materialize_source_tree,
)
from libmpv_build.toolchains import resolve_android_ndk


def _make_tar_gz(path: Path, entries: list[tuple[str, bytes | str, str]]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, payload, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = (
                    payload if isinstance(payload, bytes) else payload.encode("utf-8")
                )
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = str(payload)
                tf.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = str(payload)
                tf.addfile(info)
            elif kind == "directory":
                info.type = tarfile.DIRTYPE
                info.mode = int(payload)
                tf.addfile(info)


class FsAndCacheTests(unittest.TestCase):
    def test_git_reference_cannot_disable_fetch_head_update_and_reuse_stale_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upstream = root / "upstream"
            upstream.mkdir()

            def git(*args: str) -> str:
                return subprocess.check_output(
                    ["git", "-C", str(upstream), *args],
                    stderr=subprocess.PIPE,
                    text=True,
                ).strip()

            git("init", "-q")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            (upstream / "file").write_bytes(b"old")
            git("add", "file")
            git("commit", "-qm", "old")
            git("tag", "v1")
            older = git("rev-parse", "HEAD")
            (upstream / "file").write_bytes(b"new")
            git("commit", "-qam", "new")
            current = git("rev-parse", "HEAD")
            config = root / "gitconfig"
            config.write_text(
                f'[url "{upstream.as_uri()}"]\n    insteadOf = https://example.test/project.git\n',
                encoding="utf-8",
            )
            spec = {"repository": "https://example.test/project.git"}
            with (
                mock.patch.dict(
                    os.environ,
                    {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"},
                ),
                mock.patch(
                    "libmpv_build.sources.run_retry",
                    side_effect=lambda command: run_retry(command, attempts=1),
                ),
            ):
                self.assertEqual(
                    fetch_commit("test", spec, root / "cache", "v1")[1], older
                )
                with self.assertRaises(RuntimeError):
                    fetch_commit("test", spec, root / "cache", "--no-write-fetch-head")
                self.assertEqual(
                    fetch_commit("test", spec, root / "cache", "HEAD")[1], current
                )

    def test_safe_extract_rejects_existing_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            (outside / "file").write_bytes(b"original")
            destination = root / "destination"
            destination.mkdir()
            (destination / "link").symlink_to(outside, target_is_directory=True)
            archive = root / "input.tar.gz"
            _make_tar_gz(archive, [("link/file", b"overwritten", "file")])
            with self.assertRaises(ValueError):
                extract_archive_safe(archive, destination)
            self.assertEqual((outside / "file").read_bytes(), b"original")

    def test_replace_directory_restores_previous_content_after_failed_promotion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            destination = root / "cache"
            destination.mkdir()
            (destination / "good").write_bytes(b"previous")
            staging = root / "staging"
            staging.mkdir()
            (staging / "good").write_bytes(b"new")
            original_replace = Path.replace

            def replace(path: Path, target: Path) -> Path:
                if path == staging:
                    raise OSError("promotion failed")
                return original_replace(path, target)

            with (
                mock.patch.object(Path, "replace", replace),
                self.assertRaisesRegex(OSError, "promotion failed"),
            ):
                atomic_replace_dir(staging, destination)
            self.assertEqual((destination / "good").read_bytes(), b"previous")
            self.assertEqual((staging / "good").read_bytes(), b"new")

    def test_safe_extract_rejects_traversal_and_escaping_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bad_trav = tdp / "trav.tar.gz"
            _make_tar_gz(bad_trav, [("../escape.txt", b"x", "file")])
            with self.assertRaises(ValueError):
                extract_archive_safe(bad_trav, tdp / "out1")

            bad_sym = tdp / "sym.tar.gz"
            _make_tar_gz(bad_sym, [("pkg/sub/link", "../../outside", "symlink")])
            with self.assertRaises(ValueError):
                extract_archive_safe(bad_sym, tdp / "out2", strip_prefix="pkg")

    def test_safe_extract_rejects_duplicate_members_and_links_used_as_parents(
        self,
    ) -> None:
        cases = (
            [("pkg/file", b"one", "file"), ("pkg/file", b"two", "file")],
            [("pkg/link", "dir", "symlink"), ("pkg/link/file", b"data", "file")],
            [("pkg/link/file", b"data", "file"), ("pkg/link", "dir", "symlink")],
            [
                ("pkg/first", "pkg/second", "hardlink"),
                ("pkg/second", "pkg/first", "hardlink"),
            ],
        )
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                archive = root / "input.tar.gz"
                _make_tar_gz(archive, entries)
                with self.assertRaises(ValueError):
                    extract_archive_safe(
                        archive, root / "destination", strip_prefix="pkg"
                    )
                with self.assertRaises(ValueError):
                    archive_hash_tree(archive, strip_prefix="pkg")

    def test_safe_extract_restores_forward_hardlink_chains(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "input.tar.gz"
            _make_tar_gz(
                archive,
                [
                    ("pkg/first", "pkg/second", "hardlink"),
                    ("pkg/second", "pkg/file", "hardlink"),
                    ("pkg/file", b"data", "file"),
                ],
            )
            destination = root / "destination"
            extract_archive_safe(archive, destination, strip_prefix="pkg")
            self.assertEqual((destination / "first").read_bytes(), b"data")
            self.assertTrue(
                verify_tree_manifest(
                    destination,
                    capture_tree_manifest(destination),
                    strict_hashes=archive_hash_tree(archive, strip_prefix="pkg"),
                )
            )

    def test_manifest_fast_hit_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            arc = tdp / "ok.tar.gz"
            _make_tar_gz(arc, [("pkg/hello.txt", b"hello", "file")])
            dest = tdp / "extracted"
            dest.mkdir()
            extract_archive_safe(arc, dest, strip_prefix="pkg")
            manifest = capture_tree_manifest(dest)
            hashes = archive_hash_tree(arc, strip_prefix="pkg")

            self.assertTrue(verify_tree_manifest(dest, manifest))
            self.assertTrue(verify_tree_manifest(dest, manifest, strict_hashes=hashes))

            # Tamper size -> fast manifest fails
            (dest / "hello.txt").write_bytes(b"hello!!")
            self.assertFalse(verify_tree_manifest(dest, manifest))

            # Extra untracked file -> fast manifest fails
            (dest / "hello.txt").write_bytes(b"hello")
            (dest / "extra.txt").write_bytes(b"x")
            self.assertFalse(verify_tree_manifest(dest, manifest))

    def test_manifest_detects_directory_permission_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            child = directory / "include"
            child.mkdir(mode=0o755)
            manifest = capture_tree_manifest(directory)
            child.chmod(0o700)
            self.assertFalse(verify_tree_manifest(directory, manifest))

    def test_directory_modes_match_archive_in_strict_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "input.tar.gz"
            _make_tar_gz(
                archive,
                [
                    ("pkg/private", str(0o700), "directory"),
                    ("pkg/private/nested/file", b"data", "file"),
                ],
            )
            destination = root / "destination"
            previous_umask = os.umask(0o077)
            try:
                extract_archive_safe(archive, destination, strip_prefix="pkg")
            finally:
                os.umask(previous_umask)
            self.assertEqual(
                stat.S_IMODE((destination / "private").stat().st_mode), 0o700
            )
            self.assertEqual(
                stat.S_IMODE((destination / "private/nested").stat().st_mode), 0o755
            )
            hashes = archive_hash_tree(archive, strip_prefix="pkg")
            self.assertTrue(
                verify_tree_manifest(
                    destination,
                    capture_tree_manifest(destination),
                    strict_hashes=hashes,
                )
            )
            (destination / "private").chmod(0o755)
            self.assertFalse(
                verify_tree_manifest(
                    destination,
                    capture_tree_manifest(destination),
                    strict_hashes=hashes,
                )
            )

    def test_recursive_source_restoration_matches_locked_directory_modes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parent = root / "parent.tar.gz"
            child = root / "child.tar.gz"
            _make_tar_gz(
                parent,
                [
                    ("parent/subprojects", str(0o755), "directory"),
                    ("parent/subprojects/child", str(0o755), "directory"),
                ],
            )
            _make_tar_gz(child, [("child/nested/file", b"data", "file")])
            lock = {
                "dependencies": {
                    "parent": {"commit": "a" * 40, "sha256": "b" * 64},
                    "child": {
                        "commit": "c" * 40,
                        "sha256": "d" * 64,
                        "pinFrom": {
                            "type": "gitlink",
                            "parent": "parent",
                            "path": "subprojects/child",
                        },
                    },
                }
            }
            with mock.patch(
                "libmpv_build.sources.verified_source_archive",
                side_effect=lambda lock, name, cache: root / f"{name}.tar.gz",
            ):
                result = ensure_source_tree(
                    lock, "parent", root / "cache", verify_cache=True
                )
                self.assertEqual(
                    (result / "subprojects/child/nested/file").read_bytes(), b"data"
                )
                self.assertEqual(
                    stat.S_IMODE((result / "subprojects/child").stat().st_mode), 0o755
                )
                self.assertEqual(
                    ensure_source_tree(
                        lock, "parent", root / "cache", verify_cache=True
                    ),
                    result,
                )

    def test_strict_manifest_detects_same_metadata_content_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "input.tar.gz"
            _make_tar_gz(archive, [("pkg/file", b"hello", "file")])
            destination = root / "destination"
            extract_archive_safe(archive, destination, strip_prefix="pkg")
            path = destination / "file"
            previous = path.stat()
            manifest = capture_tree_manifest(destination)
            hashes = archive_hash_tree(archive, strip_prefix="pkg")
            path.write_bytes(b"other")
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            self.assertTrue(verify_tree_manifest(destination, manifest))
            self.assertFalse(
                verify_tree_manifest(destination, manifest, strict_hashes=hashes)
            )

    def test_source_cache_recovers_from_malformed_marker(self) -> None:
        commit = "a" * 40
        for marker in (
            [],
            {
                "schemaVersion": 3,
                "inputs": {"pkg": {"commit": commit, "sha256": "b" * 64}},
            },
        ):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                cache = root / "cache"
                directory = cache / "sources" / f"pkg-{commit}"
                directory.mkdir(parents=True)
                (directory / ".libmpv-source.json").write_text(
                    json.dumps(marker), encoding="utf-8"
                )
                archive = root / "input.tar.gz"
                _make_tar_gz(archive, [("pkg/file", b"hello", "file")])
                lock = {"dependencies": {"pkg": {"commit": commit, "sha256": "b" * 64}}}
                with mock.patch(
                    "libmpv_build.sources.verified_source_archive", return_value=archive
                ):
                    actual = ensure_source_tree(lock, "pkg", cache)
                self.assertEqual((actual / "file").read_bytes(), b"hello")

    def test_parent_source_restore_waits_for_shared_submodule_lock(self) -> None:
        lock = {
            "dependencies": {
                "parent": {"commit": "a" * 40},
                "child": {
                    "commit": "b" * 40,
                    "pinFrom": {
                        "type": "gitlink",
                        "parent": "parent",
                        "path": "subprojects/child",
                    },
                },
            }
        }
        requested = Event()
        restored = Event()
        child_key = "child:" + "b" * 40

        @contextmanager
        def observe_lock(cache_root: Path, namespace: str, key: str):
            if key == child_key:
                requested.set()
            with cache_lock(cache_root, namespace, key):
                yield

        def restore(*args, **kwargs):
            restored.set()
            return Path("restored")

        with (
            tempfile.TemporaryDirectory() as td,
            ThreadPoolExecutor(max_workers=1) as worker,
        ):
            cache = Path(td)
            with (
                mock.patch("libmpv_build.sources.cache_lock", observe_lock),
                mock.patch(
                    "libmpv_build.sources._restore_source_tree",
                    side_effect=restore,
                ),
            ):
                with cache_lock(cache, "source", child_key):
                    pending = worker.submit(ensure_source_tree, lock, "parent", cache)
                    self.assertTrue(
                        requested.wait(timeout=2),
                        "parent never locked the shared child cache",
                    )
                    self.assertFalse(restored.is_set())
                self.assertEqual(pending.result(timeout=2), Path("restored"))

    def test_source_materialization_retains_submodule_lock_until_copy_completes(
        self,
    ) -> None:
        lock = {
            "dependencies": {
                "parent": {"commit": "a" * 40},
                "child": {
                    "commit": "b" * 40,
                    "pinFrom": {
                        "type": "gitlink",
                        "parent": "parent",
                        "path": "subprojects/child",
                    },
                },
            }
        }
        claimed: set[str] = set()

        @contextmanager
        def observe_lock(cache_root: Path, namespace: str, key: str):
            with cache_lock(cache_root, namespace, key):
                claimed.add(key)
                try:
                    yield
                finally:
                    claimed.remove(key)

        def copy(directory: Path, destination: Path, *, symlinks: bool):
            self.assertEqual(directory, Path("verified parent"))
            self.assertTrue(symlinks)
            self.assertEqual(claimed, {"parent:" + "a" * 40, "child:" + "b" * 40})

        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch("libmpv_build.sources.cache_lock", observe_lock),
                mock.patch(
                    "libmpv_build.sources._restore_source_tree",
                    return_value=Path("verified parent"),
                ),
                mock.patch("libmpv_build.sources.shutil.copytree", side_effect=copy),
            ):
                destination = Path(td) / "materialized"
                self.assertEqual(
                    materialize_source_tree(lock, "parent", Path(td), destination),
                    destination,
                )
            self.assertFalse(claimed)

    def test_android_ndk_resolution_priority_without_hardcoded_path(self) -> None:
        lock = load_lock()
        rev = lock.toolchains["android"].raw["revision"]
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            env_ndk = tdp / "env-ndk"
            env_ndk.mkdir()
            (env_ndk / "source.properties").write_text(
                f"Pkg.Revision = {rev}\n", encoding="utf-8"
            )

            arg_ndk = tdp / "arg-ndk"
            arg_ndk.mkdir()
            (arg_ndk / "source.properties").write_text(
                f"Pkg.Revision = {rev}\n", encoding="utf-8"
            )

            with mock.patch.dict(
                os.environ, {"ANDROID_NDK_ROOT": str(env_ndk)}, clear=True
            ):
                self.assertEqual(
                    resolve_android_ndk(lock, None, tdp / "cache"), env_ndk.resolve()
                )
                self.assertEqual(
                    resolve_android_ndk(lock, str(arg_ndk), tdp / "cache"),
                    arg_ndk.resolve(),
                )

            bad_ndk = tdp / "bad-ndk"
            bad_ndk.mkdir()
            (bad_ndk / "source.properties").write_text(
                "Pkg.Revision = 1.0.0\n", encoding="utf-8"
            )
            with self.assertRaises(RuntimeError):
                resolve_android_ndk(lock, str(bad_ndk), tdp / "cache")


if __name__ == "__main__":
    unittest.main()
