from __future__ import annotations

import copy
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from libmpv_build import ROOT
from libmpv_build.config import load_lock
from libmpv_build.release_notes import generate_release_notes, resolve_baseline_lock
from libmpv_build.update import (
    latest_tag,
    resolve_children,
    resolve_one,
    run_update,
    sync_mpv_headers,
    update_lock,
)


class UpdateAndReleaseNotesTests(unittest.TestCase):
    def test_latest_tag_selects_highest_numeric_release(self) -> None:
        tags = (
            "aaa\trefs/tags/v0.41.0\n"
            "bbb\trefs/tags/v0.42.0-rc1\n"
            "ccc\trefs/tags/v0.42.0\n"
            "ddd\trefs/tags/v0.42.0^{}\n"
            "eee\trefs/tags/v0.42.1\n"
        )
        tag, ver = latest_tag(
            {
                "repository": "https://example.com/mpv.git",
                "tagPattern": r"v(\d+\.\d+\.\d+)",
            },
            tags,
        )
        self.assertEqual((tag, ver), ("v0.42.1", "0.42.1"))

    def test_dependency_updates_increment_revision_and_mpv_updates_reset_it(self) -> None:
        for revision, changes, expected in (
            (1, {"ffmpeg": {"commit": "b" * 40}}, "1.2.3-r2"),
            (9, {"ffmpeg": {"commit": "b" * 40}}, "1.2.3-r10"),
            (9, {"mpv": {"version": "1.3.0"}}, "1.3.0-r1"),
            (9, {"mpv": {"commit": "b" * 40}}, "1.2.3-r10"),
        ):
            raw = {
                "release": f"1.2.3-r{revision}",
                "dependencies": {
                    "mpv": {
                        "version": "1.2.3",
                        "repository": "https://example.test/mpv.git",
                    },
                    "ffmpeg": {
                        "commit": "a" * 40,
                        "repository": "https://example.test/ffmpeg.git",
                    },
                },
            }

            def resolve(item, _cache, changes=changes):
                name, record = item
                return name, {**record, **changes.get(name, {})}

            with (
                self.subTest(revision=revision, changes=changes),
                mock.patch("libmpv_build.update.resolve_one", side_effect=resolve),
                mock.patch("libmpv_build.update.resolve_children"),
            ):
                updated = update_lock(raw, Path("unused"))
                self.assertEqual(updated["release"], expected)

    def test_update_validates_repository_before_invoking_git(self) -> None:
        with mock.patch("libmpv_build.update.run_retry") as git:
            with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                resolve_one(
                    (
                        "mpv",
                        {
                            "repository": "--upload-pack=unexpected",
                            "tag": "v0.42.0",
                        },
                    ),
                    Path("unused"),
                )
            git.assert_not_called()

    def test_release_notes_renders_diff_and_checksums(self) -> None:
        current = load_lock(ROOT / "dependencies.json").raw
        baseline = copy.deepcopy(current)
        baseline["dependencies"]["ffmpeg"]["version"] = "9.0"
        baseline["dependencies"]["bzip2"] = {"version": "1.0.8", "commit": "0" * 40}

        notes = generate_release_notes(
            current,
            "abc123  libmpv-0.42.0-r6-linux-x64.zip\n",
            release="0.42.0-r6",
            commit="deadbeef",
            baseline=baseline,
            baseline_label="0.42.0-r5",
        )
        self.assertIn("| ffmpeg | 9.0 | 9.1 |", notes)
        self.assertIn("| bzip2 | 1.0.8 | （移除） |", notes)
        self.assertIn("libmpv-0.42.0-r6-linux-x64.zip", notes)

    def _write_headers_archive(self, archive: Path, names: set[str]) -> None:
        with tarfile.open(archive, "w:gz") as output:
            for name in sorted(names):
                data = f"new {name}".encode()
                entry = tarfile.TarInfo(f"mpv/include/mpv/{name}")
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))

    def test_header_sync_replaces_changed_and_removed_headers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            headers = root / "include/mpv"
            headers.mkdir(parents=True)
            (headers / "client.h").write_bytes(b"old")
            (headers / "retired.h").write_bytes(b"retired")
            names = {"client.h", "render.h", "render_gl.h", "stream_cb.h"}
            archive = root / "mpv.tar.gz"
            self._write_headers_archive(archive, names)
            with mock.patch(
                "libmpv_build.update.verified_source_archive", return_value=archive
            ):
                changed = sync_mpv_headers({}, root / "cache", root)
            self.assertEqual({p.name for p in changed}, names | {"retired.h"})
            self.assertEqual({p.name for p in headers.iterdir()}, names)
            self.assertEqual((headers / "client.h").read_bytes(), b"new client.h")

    def test_update_header_failure_leaves_original_lock_and_headers(self) -> None:
        for failure in ("archive", "lock-write"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                lock_path = root / "dependencies.json"
                original = load_lock().raw
                original_text = json.dumps(original) + "\n"
                lock_path.write_text(original_text, encoding="utf-8")
                headers = root / "include/mpv"
                headers.mkdir(parents=True)
                (headers / "client.h").write_bytes(b"previous header")
                candidate = copy.deepcopy(original)
                candidate["dependencies"]["mpv"]["commit"] = "0" * 40
                archive = root / "mpv.tar.gz"
                self._write_headers_archive(
                    archive, {"client.h", "render.h", "render_gl.h", "stream_cb.h"}
                )
                with (
                    mock.patch(
                        "libmpv_build.update.update_lock", return_value=candidate
                    ),
                    mock.patch(
                        "libmpv_build.update.verified_source_archive",
                        **(
                            {"side_effect": ValueError("bad archive")}
                            if failure == "archive"
                            else {"return_value": archive}
                        ),
                    ),
                    mock.patch(
                        "libmpv_build.update.atomic_write_text",
                        side_effect=OSError("disk full"),
                    ),
                    self.assertRaises((ValueError, OSError)),
                ):
                    run_update(
                        lock_path=lock_path,
                        cache_root=root / "cache",
                        root=root,
                        write=True,
                    )
                self.assertEqual(lock_path.read_text(encoding="utf-8"), original_text)
                self.assertEqual(
                    {p.name: p.read_bytes() for p in headers.iterdir()},
                    {"client.h": b"previous header"},
                )

    def test_invalid_candidate_does_not_write_lock_headers_or_workflow_outputs(
        self,
    ) -> None:
        original = load_lock().raw
        for field, value in (
            ("sha256", "invalid"),
            ("date", "2026-11-24"),
            ("targets", ["unknown-target"]),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                lock_path = root / "dependencies.json"
                original_text = json.dumps(original) + "\n"
                lock_path.write_text(original_text, encoding="utf-8")
                headers = root / "include/mpv"
                headers.mkdir(parents=True)
                (headers / "client.h").write_bytes(b"previous header")
                workflow_output = root / "github-output"
                workflow_output.write_text("previous=value\n", encoding="utf-8")
                candidate = copy.deepcopy(original)
                candidate["dependencies"]["mpv"][field] = value
                with (
                    mock.patch(
                        "libmpv_build.update.update_lock", return_value=candidate
                    ),
                    mock.patch(
                        "libmpv_build.update.verified_source_archive"
                    ) as archive,
                    self.assertRaises(ValueError),
                ):
                    run_update(
                        lock_path=lock_path,
                        cache_root=root / "cache",
                        root=root,
                        write=True,
                        github_output=workflow_output,
                    )
                archive.assert_not_called()
                self.assertEqual(lock_path.read_text(encoding="utf-8"), original_text)
                self.assertEqual(
                    (headers / "client.h").read_bytes(), b"previous header"
                )
                self.assertEqual(
                    workflow_output.read_text(encoding="utf-8"), "previous=value\n"
                )

    def test_header_update_rejects_symlinked_repository_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            outside = Path(td) / "outside"
            headers = outside / "mpv"
            headers.mkdir(parents=True)
            (headers / "client.h").write_bytes(b"unrelated outside data")
            (root / "include").symlink_to(outside, target_is_directory=True)
            lock_path = root / "dependencies.json"
            original = load_lock().raw
            original_text = json.dumps(original) + "\n"
            lock_path.write_text(original_text, encoding="utf-8")
            candidate = copy.deepcopy(original)
            candidate["dependencies"]["mpv"]["commit"] = "0" * 40
            with (
                mock.patch("libmpv_build.update.update_lock", return_value=candidate),
                mock.patch("libmpv_build.update.verified_source_archive") as archive,
                self.assertRaisesRegex(ValueError, "ordinary directory"),
            ):
                run_update(
                    lock_path=lock_path,
                    cache_root=root / "cache",
                    root=root,
                    write=True,
                )
            archive.assert_not_called()
            self.assertEqual(lock_path.read_text(encoding="utf-8"), original_text)
            self.assertEqual(
                (headers / "client.h").read_bytes(), b"unrelated outside data"
            )
            self.assertEqual({p.name for p in headers.iterdir()}, {"client.h"})

    def test_update_commit_includes_header_additions_and_deletions_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def git(*args: str) -> str:
                return subprocess.check_output(
                    ["git", "-C", str(root), *args], text=True
                ).strip()

            git("init", "--quiet")
            git("config", "user.name", "Update test")
            git("config", "user.email", "test@example.invalid")
            lock_path = root / "dependencies.json"
            original = load_lock().raw
            lock_path.write_text(json.dumps(original), encoding="utf-8")
            headers = root / "include/mpv"
            headers.mkdir(parents=True)
            (headers / "client.h").write_bytes(b"previous header")
            (headers / "retired.h").write_bytes(b"retired")
            unrelated = root / "unrelated.txt"
            unrelated.write_bytes(b"previous")
            git("add", "dependencies.json", "include/mpv", "unrelated.txt")
            git("commit", "--quiet", "-m", "Initial fixture")
            unrelated.write_bytes(b"staged user work")
            git("add", "unrelated.txt")

            candidate = copy.deepcopy(original)
            candidate["dependencies"]["mpv"]["commit"] = "0" * 40
            archive = root / "mpv.tar.gz"
            names = {"client.h", "render.h", "render_gl.h", "stream_cb.h"}
            self._write_headers_archive(archive, names)
            with (
                mock.patch("libmpv_build.update.update_lock", return_value=candidate),
                mock.patch(
                    "libmpv_build.update.verified_source_archive",
                    return_value=archive,
                ),
            ):
                result = run_update(
                    lock_path=lock_path,
                    cache_root=root / "cache",
                    root=root,
                    commit=True,
                )
            committed = set(
                git(
                    "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
                ).splitlines()
            )
            self.assertEqual(
                committed,
                {"dependencies.json"}
                | {f"include/mpv/{name}" for name in names | {"retired.h"}},
            )
            self.assertEqual(git("diff", "--cached", "--name-only"), "unrelated.txt")
            self.assertEqual(set(result["headersUpdated"]), names | {"retired.h"})

    def test_header_sync_rejects_incomplete_archive_without_deleting_existing_headers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            headers = root / "include/mpv"
            headers.mkdir(parents=True)
            (headers / "client.h").write_bytes(b"old")
            archive = root / "mpv.tar.gz"
            self._write_headers_archive(archive, {"client.h"})
            with (
                mock.patch(
                    "libmpv_build.update.verified_source_archive", return_value=archive
                ),
                self.assertRaisesRegex(ValueError, "incomplete"),
            ):
                sync_mpv_headers({}, root / "cache", root)
            self.assertEqual((headers / "client.h").read_bytes(), b"old")

    def test_release_notes_falls_back_when_github_cli_is_unavailable(self) -> None:
        baseline = {"dependencies": {}}
        with mock.patch(
            "libmpv_build.release_notes.subprocess.run",
            side_effect=[
                FileNotFoundError("gh"),
                subprocess.CompletedProcess([], 0, json.dumps(baseline), ""),
            ],
        ) as run:
            self.assertEqual(resolve_baseline_lock(), (baseline, "HEAD~1"))
        self.assertIn("HEAD~1:dependencies.json", run.call_args.args[0])

    def test_update_rejects_removed_required_gitlink_instead_of_reusing_old_pin(
        self,
    ) -> None:
        lock = {
            "dependencies": {
                "freetype": {"commit": "1" * 40},
                "freetype_dlg": {
                    "commit": "2" * 40,
                    "pinFrom": {"parent": "freetype", "path": "subprojects/dlg"},
                },
            }
        }
        with (
            mock.patch("libmpv_build.update.gitlinks", return_value=[]),
            self.assertRaisesRegex(ValueError, "missing required pinned submodules"),
        ):
            resolve_children(lock, Path("unused-cache"))

    def test_update_removes_stale_gitlink_records_before_repinning_parent(self) -> None:
        lock = {
            "dependencies": {
                "freetype": {
                    "commit": "1" * 40,
                    "targets": ["linux-x64"],
                },
                "freetype_old": {
                    "commit": "2" * 40,
                    "pinFrom": {
                        "type": "gitlink",
                        "parent": "freetype",
                        "path": "subprojects/old",
                    },
                },
            }
        }
        module = {
            "path": "subprojects/dlg",
            "repository": "https://example.com/dlg.git",
            "commit": "3" * 40,
        }
        with (
            mock.patch("libmpv_build.update.gitlinks", return_value=[module]),
            mock.patch(
                "libmpv_build.update.resolve_dependency",
                return_value={"commit": "3" * 40},
            ),
        ):
            resolve_children(lock, Path("unused-cache"))
        self.assertNotIn("freetype_old", lock["dependencies"])
        self.assertEqual(lock["dependencies"]["freetype_dlg"]["commit"], "3" * 40)

    def test_update_rejects_pin_name_collision_without_replacing_root_dependency(
        self,
    ) -> None:
        root_record = {"repository": "https://example.test/independent.git"}
        lock = {
            "dependencies": {
                "freetype": {"commit": "1" * 40, "targets": ["linux-x64"]},
                "freetype_dlg": root_record.copy(),
            }
        }
        with (
            mock.patch(
                "libmpv_build.update.gitlinks",
                return_value=[{
                    "path": "subprojects/dlg",
                    "repository": "https://example.test/dlg.git",
                    "commit": "2" * 40,
                }],
            ),
            mock.patch("libmpv_build.update.resolve_dependency") as resolve,
            self.assertRaisesRegex(ValueError, "Pinned dependency name collision"),
        ):
            resolve_children(lock, Path("unused-cache"))
        resolve.assert_not_called()
        self.assertEqual(lock["dependencies"]["freetype_dlg"], root_record)

    def test_workflow_release_condition_respects_manual_publish_switch(self) -> None:
        workflow = (ROOT / ".github/workflows/native.yml").read_text(encoding="utf-8")
        release = workflow.split("\n  release:\n", 1)[1]
        expression = next(
            line.strip()[4:]
            for line in release.splitlines()
            if line.strip().startswith("if: ")
        )
        expression = expression.replace("&&", " and ").replace("||", " or ")
        for event, changed, publish, expected in (
            ("workflow_dispatch", "true", False, False),
            ("workflow_dispatch", "false", False, False),
            ("workflow_dispatch", "true", True, True),
            ("workflow_dispatch", "false", True, True),
            ("schedule", "true", False, True),
            ("schedule", "false", False, False),
            ("push", "true", False, False),
        ):
            with self.subTest(event=event, changed=changed, publish=publish):
                context = {
                    "github": SimpleNamespace(event_name=event),
                    "needs": SimpleNamespace(
                        update=SimpleNamespace(outputs=SimpleNamespace(changed=changed))
                    ),
                    "inputs": SimpleNamespace(publish=publish),
                    "true": True,
                }
                self.assertEqual(
                    eval(expression, {"__builtins__": {}}, context), expected
                )

    def test_workflow_update_waits_for_unit_tests_before_using_write_permissions(
        self,
    ) -> None:
        workflow = (ROOT / ".github/workflows/native.yml").read_text(encoding="utf-8")
        update = workflow.split("\n  update:\n", 1)[1].split("\n  build:\n", 1)[0]
        self.assertIn("    needs: test", update.splitlines())
        self.assertIn("\n      contents: write\n", update)

    def test_update_lock_preserves_tarball_dependencies_without_git(self) -> None:
        raw = load_lock(ROOT / "dependencies.json").raw
        with (
            mock.patch(
                "libmpv_build.update.resolve_one",
                side_effect=lambda item, _cache: item,
            ) as resolve_git,
            mock.patch("libmpv_build.update.resolve_children"),
        ):
            updated = update_lock(raw, Path("unused"))
        called_names = {call.args[0][0] for call in resolve_git.call_args_list}
        self.assertNotIn("libiconv", called_names)
        self.assertEqual(updated, raw)


if __name__ == "__main__":
    unittest.main()
