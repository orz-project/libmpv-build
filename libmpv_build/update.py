"""Resolve upstream stable releases, sync mpv public headers, and update dependencies.json."""

from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ROOT
from .config import BUILD_CONFIG, load_lock, validate_https_url
from .fs import atomic_replace_dir, atomic_write_text, extract_archive_safe
from .locks import file_lock
from .process import run_retry
from .sources import fetch_commit, gitlinks, resolve_dependency, verified_source_archive


def latest_tag(specification: dict, tags: str) -> tuple[str, str]:
    pattern = re.compile(specification["tagPattern"])
    candidates: list[tuple[tuple[int, ...], str, str]] = []
    for line in tags.splitlines():
        if "\trefs/tags/" not in line or line.endswith("^{}"):
            continue
        tag = line.split("\trefs/tags/", 1)[1]
        match = pattern.fullmatch(tag)
        if match:
            version = match.group(1).replace("_", ".").replace("-", ".")
            candidates.append(
                (tuple(int(part) for part in version.split(".")), tag, version)
            )
    if not candidates:
        raise ValueError(f"No stable release matches {specification['repository']}")
    _, tag, version = max(candidates)
    return tag, version


def resolve_one(item: tuple[str, dict], cache: Path, initial: bool) -> tuple[str, dict]:
    name, specification = item
    validate_https_url(specification["repository"], f"Dependency {name}")
    if not initial:
        tags = run_retry(["git", "ls-remote", "--tags", specification["repository"]])
        tag, version = latest_tag(specification, tags)
        if (
            tag == specification.get("tag")
            and specification.get("commit")
            and specification.get("sha256")
        ):
            return name, specification
        specification = {**specification, "tag": tag, "version": version}
    print(f"Resolving {name} {specification['tag']}", flush=True)
    return name, resolve_dependency(name, specification, cache)


def resolve_children(lock_raw: dict, cache: Path) -> None:
    """Pin build-required Git submodules and shaderc DEPS to their released parent."""
    dependencies = lock_raw["dependencies"]
    needed_gitlinks = BUILD_CONFIG.update.required_gitlinks
    for parent, wanted in needed_gitlinks.items():
        if parent not in dependencies:
            continue
        # Rebuild the parent's complete pinned-child set on every update.  A
        # released parent may drop a submodule; retaining the old record would
        # make source_children() materialize stale code at that path.
        for child, record in list(dependencies.items()):
            pin = record.get("pinFrom")
            if (
                child != parent
                and isinstance(pin, dict)
                and pin.get("type") == "gitlink"
                and pin.get("parent") == parent
            ):
                del dependencies[child]
        found: set[str] = set()
        for module in gitlinks(parent, dependencies[parent], cache):
            if module["path"] not in wanted:
                continue
            found.add(module["path"])
            name = f"{parent}_{Path(module['path']).name.lower()}".replace("-", "_")
            record = {
                "repository": module["repository"],
                "tag": None,
                "version": f"git-{module['commit'][:12]}",
                "targets": dependencies[parent]["targets"],
                "pinFrom": {
                    "type": "gitlink",
                    "parent": parent,
                    "path": module["path"],
                },
            }
            existing = dependencies.get(name)
            if existing is not None and existing.get("pinFrom") != record["pinFrom"]:
                raise ValueError(
                    f"Pinned dependency name collision: {name}"
                )
            print(f"Resolving {name} {module['commit'][:12]}", flush=True)
            dependencies[name] = resolve_dependency(
                name, record, cache, module["commit"]
            )
        if found != wanted:
            raise ValueError(
                f"Released {parent} is missing required pinned submodules: {sorted(wanted - found)}"
            )

    if "shaderc" in dependencies:
        for child, record in list(dependencies.items()):
            pin = record.get("pinFrom")
            if (
                isinstance(pin, dict)
                and pin.get("type") == "deps"
                and pin.get("parent") == "shaderc"
            ):
                del dependencies[child]
        directory, commit, _ = fetch_commit(
            "shaderc", dependencies["shaderc"], cache, dependencies["shaderc"]["commit"]
        )
        contents = run_retry(["git", "-C", str(directory), "show", f"{commit}:DEPS"])
        for name, variable, repository in BUILD_CONFIG.update.shaderc_deps:
            match = re.search(
                rf"['\"]{variable}['\"]\s*:\s*['\"]([0-9a-f]{{40}})['\"]", contents
            )
            if not match:
                raise ValueError(f"Missing {variable} in released shaderc DEPS")
            child = match.group(1)
            record = {
                "repository": repository,
                "tag": None,
                "version": f"git-{child[:12]}",
                "targets": ["windows-x64"],
                "pinFrom": {"type": "deps", "parent": "shaderc", "variable": variable},
            }
            print(f"Resolving {name} {child[:12]}", flush=True)
            dependencies[name] = resolve_dependency(name, record, cache, child)


def sync_mpv_headers(
    lock_raw: dict, cache_root: Path, repo_root: Path = ROOT
) -> list[Path]:
    """Extract include/mpv/*.h from the locked mpv archive and atomically sync repo include/mpv/."""
    _check_header_destination(repo_root)
    archive = verified_source_archive(lock_raw, "mpv", cache_root)
    repo_headers = repo_root / "include/mpv"
    repo_headers.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".mpv-headers-", dir=repo_headers.parent
    ) as tmpdir:
        tmp_path = Path(tmpdir)
        extract_archive_safe(archive, tmp_path, strip_prefix="mpv")
        source_headers = tmp_path / "include/mpv"
        if not source_headers.is_dir():
            raise ValueError("Locked mpv archive is missing include/mpv")
        headers = sorted(source_headers.glob("*.h"))
        required = {"client.h", "render.h", "render_gl.h", "stream_cb.h"}
        if not required.issubset({header.name for header in headers}) or any(
            header.is_symlink() or not header.is_file() for header in headers
        ):
            raise ValueError(
                "Locked mpv archive has incomplete or non-regular public headers"
            )
        staging = tmp_path / "staged-headers"
        staging.mkdir()
        for header in headers:
            shutil.copyfile(header, staging / header.name)
        changed: list[Path] = []
        target_names = {p.name for p in staging.glob("*.h")}
        existing_names = (
            {p.name for p in repo_headers.glob("*.h")}
            if repo_headers.is_dir()
            else set()
        )
        for name in sorted(target_names | existing_names):
            dest = repo_headers / name
            src = staging / name
            if (
                not src.is_file()
                or dest.is_symlink()
                or not dest.is_file()
                or src.read_bytes() != dest.read_bytes()
            ):
                changed.append(dest)
        if changed:
            atomic_replace_dir(staging, repo_headers)
        return changed


def _check_header_destination(root: Path) -> None:
    """Keep public-header updates inside ordinary repository directories."""
    for path in (root / "include", root / "include/mpv"):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(
                f"Repository public header path must be an ordinary directory: {path}"
            )


def _validate_candidate(candidate: dict, root: Path) -> None:
    """Apply the build lock schema before updating files or emitting workflow outputs."""
    with tempfile.TemporaryDirectory(prefix=".validate-lock-", dir=root) as transaction:
        candidate_path = Path(transaction) / "dependencies.json"
        candidate_path.write_text(json.dumps(candidate, indent=4), encoding="utf-8")
        load_lock(candidate_path)


def update_lock(lock_raw: dict, cache: Path, initial: bool = False) -> dict:
    candidate = json.loads(json.dumps(lock_raw))
    roots = [
        (name, specification)
        for name, specification in candidate["dependencies"].items()
        if "pinFrom" not in specification and "repository" in specification
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(resolve_one, item, cache, initial) for item in roots]
        for future in concurrent.futures.as_completed(futures):
            name, specification = future.result()
            candidate["dependencies"][name] = specification
    resolve_children(candidate, cache)
    if not initial and candidate["dependencies"] != lock_raw["dependencies"]:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        identity = hashlib.sha256(
            json.dumps(candidate["dependencies"], sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        candidate["release"] = (
            f"{candidate['dependencies']['mpv']['version']}-{stamp}-{identity}"
        )
    return candidate


def _write_update(
    original_text: str,
    candidate: dict,
    lock_path: Path,
    cache_root: Path,
    root: Path,
    sync_headers: bool,
) -> list[Path]:
    repo_headers = root / "include/mpv"
    update_headers = (
        sync_headers
        and candidate["dependencies"]["mpv"]["commit"]
        != json.loads(original_text)["dependencies"]["mpv"]["commit"]
    )
    with tempfile.TemporaryDirectory(
        prefix=".dependency-update-", dir=root
    ) as transaction:
        transaction_root = Path(transaction)
        changes: list[Path] = []
        if update_headers:
            _check_header_destination(root)
            staged_headers = transaction_root / "include/mpv"
            if repo_headers.exists():
                shutil.copytree(repo_headers, staged_headers, symlinks=True)
            changes = [
                root / path.relative_to(transaction_root)
                for path in sync_mpv_headers(candidate, cache_root, transaction_root)
            ]

        if lock_path.read_text(encoding="utf-8") != original_text:
            raise ValueError(
                "Dependency lock changed during update; retry without overwriting concurrent edits"
            )
        previous_headers = transaction_root / "previous-headers"
        moved_previous = False
        installed_headers = False
        try:
            if changes:
                _check_header_destination(root)
                repo_headers.parent.mkdir(parents=True, exist_ok=True)
                if repo_headers.exists():
                    repo_headers.replace(previous_headers)
                    moved_previous = True
                (transaction_root / "include/mpv").replace(repo_headers)
                installed_headers = True
            atomic_write_text(
                lock_path, json.dumps(candidate, indent=4, ensure_ascii=False) + "\n"
            )
        except BaseException:
            if installed_headers:
                shutil.rmtree(repo_headers)
            if moved_previous:
                previous_headers.replace(repo_headers)
            raise
        return changes


def run_update(
    *,
    lock_path: Path,
    cache_root: Path,
    initial: bool = False,
    write: bool = False,
    commit: bool = False,
    github_output: Path | None = None,
    sync_headers: bool = True,
    root: Path = ROOT,
) -> dict:
    root = root.resolve()
    if commit:
        lock_path.resolve().relative_to(root)
    with file_lock(root / ".cache/locks/update.lock"):
        original_text = lock_path.read_text(encoding="utf-8")
        original = json.loads(original_text)
        if not initial:
            _validate_candidate(original, root)
        candidate = update_lock(original, cache_root, initial=initial)
        _validate_candidate(candidate, root)
        changed = original != candidate
        header_changes: list[Path] = []
        if changed and (write or commit):
            header_changes = _write_update(
                original_text, candidate, lock_path, cache_root, root, sync_headers
            )
            if commit:
                files_to_add = [str(lock_path.resolve().relative_to(root))]
                files_to_add.extend(str(p.relative_to(root)) for p in header_changes)
                subprocess.run(
                    ["git", "-C", str(root), "add", "--", *files_to_add], check=True
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "commit",
                        "--only",
                        "-m",
                        "Update stable native dependencies",
                        "--",
                        *files_to_add,
                    ],
                    check=True,
                )
    result = {
        "changed": changed,
        "dependencies": len(candidate["dependencies"]),
        "release": candidate["release"],
        "headersUpdated": [p.name for p in header_changes],
    }
    if github_output is not None:
        head_commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        with github_output.open("a", encoding="utf-8") as stream:
            stream.write(
                f"changed={str(changed).lower()}\ncommit={head_commit}\nrelease={candidate['release']}\n"
            )
    return result
