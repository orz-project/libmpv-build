"""Immutable Git source mirroring, deterministic archiving, and verified tree restoration."""

from __future__ import annotations

import configparser
import contextlib
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .config import COMMIT_RE, SHA256_RE, validate_https_url
from .fs import (
    archive_hash_tree,
    atomic_replace_dir,
    capture_tree_manifest,
    extract_archive_safe,
    sha256_file,
    verify_tree_manifest,
)
from .locks import cache_lock
from .process import run_retry

SOURCE_MARKER = ".libmpv-source.json"


def git_directory(name: str, specification: dict, cache_root: Path) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]+", name):
        raise ValueError(f"Invalid dependency name: {name}")
    repository = specification["repository"]
    validate_https_url(repository, f"Dependency {name}")
    identity = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:12]
    directory = cache_root / "git" / f"{name}-{identity}"
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        run_retry(["git", "init", "--bare", "--quiet", str(directory)])
    return directory


def fetch_commit(
    name: str, specification: dict, cache_root: Path, reference: str
) -> tuple[Path, str, str]:
    identity = specification["repository"]
    with cache_lock(cache_root, "git", f"{name}:{identity}"):
        return _fetch_commit_unlocked(name, specification, cache_root, reference)


def _fetch_commit_unlocked(
    name: str, specification: dict, cache_root: Path, reference: str
) -> tuple[Path, str, str]:
    directory = git_directory(name, specification, cache_root)
    if not COMMIT_RE.fullmatch(reference):
        run_retry(
            [
                "git",
                "-C",
                str(directory),
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "--",
                specification["repository"],
                reference,
            ]
        )
        commit = run_retry(
            ["git", "-C", str(directory), "rev-parse", "FETCH_HEAD^{commit}"]
        )
    else:
        commit = reference
        try:
            run_retry(
                ["git", "-C", str(directory), "cat-file", "-e", f"{commit}^{{commit}}"]
            )
        except RuntimeError:
            run_retry(
                [
                    "git",
                    "-C",
                    str(directory),
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "--no-tags",
                    "--",
                    specification["repository"],
                    commit,
                ]
            )
    date = run_retry(
        ["git", "-C", str(directory), "show", "-s", "--format=%cI", commit]
    )
    return directory, commit, date


def archive_source(
    name: str,
    directory: Path,
    commit: str,
    cache_root: Path,
    expected_sha256: str | None = None,
) -> Path:
    """Reuse a digest-verified archive or regenerate deterministically from the Git commit."""
    if not re.fullmatch(r"[a-z0-9_-]+", name) or not COMMIT_RE.fullmatch(commit):
        raise ValueError(f"Invalid immutable source identity: {name}/{commit}")
    if expected_sha256 is not None and not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError(f"Invalid locked source SHA-256: {name}")
    archive = cache_root / "archives" / f"{name}-{commit}.tar.gz"
    if (
        expected_sha256 is not None
        and archive.is_file()
        and not archive.is_symlink()
        and sha256_file(archive) == expected_sha256
    ):
        return archive
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=archive.parent, delete=False) as output:
        temporary = Path(output.name)
        process = subprocess.Popen(
            [
                "git",
                "-C",
                str(directory),
                "archive",
                "--format=tar",
                f"--prefix={name}/",
                commit,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with gzip.GzipFile(
                fileobj=output, filename="", mode="wb", mtime=0, compresslevel=6
            ) as compressed:
                assert process.stdout is not None
                shutil.copyfileobj(process.stdout, compressed)
            assert process.stderr is not None
            errors = process.stderr.read().decode("utf-8", errors="replace")
            if process.wait() != 0:
                raise RuntimeError(f"Cannot archive {name}: {errors}")
        except BaseException:
            process.kill()
            process.wait()
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    try:
        if expected_sha256 is not None and sha256_file(temporary) != expected_sha256:
            raise ValueError(f"Source SHA-256 mismatch: {name}")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def resolve_dependency(
    name: str, specification: dict, cache_root: Path, reference: str | None = None
) -> dict:
    directory, commit, date = fetch_commit(
        name, specification, cache_root, reference or specification["tag"]
    )
    expected = (
        specification.get("sha256") if specification.get("commit") == commit else None
    )
    archive = archive_source(name, directory, commit, cache_root, expected)
    return {
        **specification,
        "commit": commit,
        "date": date,
        "sha256": sha256_file(archive),
    }


def _tarball_suffix(url: str) -> str:
    path = urlsplit(url).path.lower()
    for suffix in (".tar.xz", ".tar.bz2", ".tar.gz", ".tgz"):
        if path.endswith(suffix):
            return suffix
    return ".tar.gz"


def _source_identity(name: str, specification: dict) -> tuple[str, str]:
    expected = specification["sha256"]
    if not SHA256_RE.fullmatch(expected):
        raise ValueError(f"Dependency is not fully locked: {name}")
    if "url" in specification:
        # Keep the complete digest in cache identities.  A truncated prefix
        # can alias two locked tarballs and make concurrent source restores
        # overwrite one another before the marker catches the mismatch.
        return expected, expected
    commit = specification["commit"]
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError(f"Dependency is not fully locked: {name}")
    return commit, expected


def verified_source_archive(lock_raw: dict, name: str, cache_root: Path) -> Path:
    """Return a SHA-256 verified source archive from cache, HTTPS tarball, or Git mirror."""
    specification = lock_raw["dependencies"][name]
    if not re.fullmatch(r"[a-z0-9_-]+", name):
        raise ValueError(f"Invalid dependency name: {name}")
    if "url" in specification:
        url = specification["url"]
        expected = specification["sha256"]
        validate_https_url(url, f"Dependency {name}")
        if not SHA256_RE.fullmatch(expected):
            raise ValueError(f"Dependency is not fully locked: {name}")
        archive = (
            cache_root
            / "archives"
            / f"{name}-{expected}{_tarball_suffix(url)}"
        )
        if (
            archive.is_file()
            and not archive.is_symlink()
            and sha256_file(archive) == expected
        ):
            return archive
        archive.parent.mkdir(parents=True, exist_ok=True)
        with cache_lock(cache_root, "tarball", f"{name}:{expected}"):
            if (
                archive.is_file()
                and not archive.is_symlink()
                and sha256_file(archive) == expected
            ):
                return archive
            with tempfile.NamedTemporaryFile(
                dir=archive.parent, prefix=f".{name}-", delete=False
            ) as target:
                temporary = Path(target.name)
                try:
                    with urllib.request.urlopen(url, timeout=120) as response:
                        shutil.copyfileobj(response, target)
                    target.flush()
                    if sha256_file(temporary) != expected:
                        raise ValueError(f"Source SHA-256 mismatch: {name}")
                    temporary.replace(archive)
                finally:
                    temporary.unlink(missing_ok=True)
        return archive

    commit = specification["commit"]
    expected = specification["sha256"]
    if not COMMIT_RE.fullmatch(commit) or not SHA256_RE.fullmatch(expected):
        raise ValueError(f"Dependency is not fully locked: {name}")
    archive = cache_root / "archives" / f"{name}-{commit}.tar.gz"
    if (
        archive.is_file()
        and not archive.is_symlink()
        and sha256_file(archive) == expected
    ):
        return archive
    directory, _, _ = fetch_commit(name, specification, cache_root, commit)
    return archive_source(name, directory, commit, cache_root, expected)


def source_children(lock_raw: dict, name: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for child, record in lock_raw["dependencies"].items():
        parent = record.get("pinFrom", {})
        if parent.get("type") == "gitlink" and parent.get("parent") == name:
            relative = PurePosixPath(parent["path"])
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Invalid submodule path: {parent['path']}")
            path = relative.as_posix()
            if any(
                path == existing
                or path.startswith(existing + "/")
                or existing.startswith(path + "/")
                for _, existing in result
            ):
                raise ValueError(f"Overlapping submodule paths: {name}/{path}")
            result.append((child, path))
    return result


def locked_source_inputs(
    lock_raw: dict, name: str, ancestors: tuple[str, ...] = ()
) -> dict[str, dict]:
    if name in ancestors:
        raise ValueError(f"Cyclic source submodule inputs: {name}")
    inputs = {name: lock_raw["dependencies"][name]}
    for child, _ in source_children(lock_raw, name):
        inputs.update(locked_source_inputs(lock_raw, child, (*ancestors, name)))
    return inputs


def locked_source_hashes(
    lock_raw: dict,
    name: str,
    cache_root: Path,
    ancestors: tuple[str, ...] = (),
) -> dict[str, dict]:
    if name in ancestors:
        raise ValueError(f"Cyclic source submodule inputs: {name}")
    strip_prefix = lock_raw["dependencies"][name].get("stripPrefix") or name
    result = archive_hash_tree(
        verified_source_archive(lock_raw, name, cache_root), strip_prefix=strip_prefix
    )
    for child, path in source_children(lock_raw, name):
        child_tree = locked_source_hashes(
            lock_raw, child, cache_root, (*ancestors, name)
        )
        root_mode = result.get(path, {}).get("mode", 0o755)
        result = {
            entry: value
            for entry, value in result.items()
            if entry != path and not entry.startswith(path + "/")
        }
        relative = PurePosixPath(path)
        for parent in (relative, *relative.parents):
            if parent.parts:
                result.setdefault(
                    parent.as_posix(),
                    {
                        "type": "directory",
                        "mode": root_mode if parent == relative else 0o755,
                    },
                )
        result.update(
            {path + "/" + entry: value for entry, value in child_tree.items()}
        )
    return result


@contextlib.contextmanager
def _locked_source_tree(
    lock_raw: dict, name: str, cache_root: Path, *, verify_cache: bool
):
    inputs = locked_source_inputs(lock_raw, name)
    with contextlib.ExitStack() as locks:
        for dependency, specification in sorted(inputs.items()):
            ident = specification.get("commit") or specification["sha256"]
            locks.enter_context(
                cache_lock(cache_root, "source", f"{dependency}:{ident}")
            )
        yield _restore_source_tree(
            lock_raw, name, cache_root, verify_cache=verify_cache
        )


def ensure_source_tree(
    lock_raw: dict, name: str, cache_root: Path, *, verify_cache: bool = False
) -> Path:
    with _locked_source_tree(
        lock_raw, name, cache_root, verify_cache=verify_cache
    ) as directory:
        return directory


def materialize_source_tree(
    lock_raw: dict,
    name: str,
    cache_root: Path,
    destination: Path,
    *,
    verify_cache: bool = False,
) -> Path:
    """Copy a verified source tree while retaining all source/submodule cache locks."""
    with _locked_source_tree(
        lock_raw, name, cache_root, verify_cache=verify_cache
    ) as directory:
        shutil.copytree(directory, destination, symlinks=True)
    return destination


def _restore_source_tree(
    lock_raw: dict, name: str, cache_root: Path, *, verify_cache: bool
) -> Path:
    specification = lock_raw["dependencies"][name]
    ident, _ = _source_identity(name, specification)
    strip_prefix = specification.get("stripPrefix") or name
    directory = cache_root / "sources" / f"{name}-{ident}"
    marker = directory / SOURCE_MARKER
    inputs = locked_source_inputs(lock_raw, name)
    if marker.is_file() and not marker.is_symlink():
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            if (
                isinstance(recorded, dict)
                and recorded.get("schemaVersion") == 3
                and recorded.get("inputs") == inputs
                and isinstance(recorded.get("manifest"), dict)
            ):
                strict = (
                    locked_source_hashes(lock_raw, name, cache_root)
                    if verify_cache
                    else None
                )
                if verify_tree_manifest(
                    directory,
                    recorded["manifest"],
                    exclude=(SOURCE_MARKER,),
                    strict_hashes=strict,
                ):
                    return directory
        except (OSError, ValueError):
            pass

    archive = verified_source_archive(lock_raw, name, cache_root)
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=directory.parent))
    try:
        extract_archive_safe(archive, staging, strip_prefix=strip_prefix)
        for child, relative in source_children(lock_raw, name):
            target = staging.joinpath(*PurePosixPath(relative).parts)
            root_mode = (
                (target.stat().st_mode & 0o777)
                if target.is_dir() and not target.is_symlink()
                else 0o755
            )
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
            child_dir = _restore_source_tree(
                lock_raw, child, cache_root, verify_cache=verify_cache
            )
            shutil.copytree(child_dir, target, symlinks=True)
            target.chmod(root_mode)
            (target / SOURCE_MARKER).unlink(missing_ok=True)
        manifest = capture_tree_manifest(staging, exclude=(SOURCE_MARKER,))
        if verify_cache:
            strict = locked_source_hashes(lock_raw, name, cache_root)
            if not verify_tree_manifest(
                staging, manifest, exclude=(SOURCE_MARKER,), strict_hashes=strict
            ):
                raise ValueError(
                    f"Materialized source differs from its locked archives: {name}"
                )
        marker_payload = {"schemaVersion": 3, "inputs": inputs, "manifest": manifest}
        (staging / SOURCE_MARKER).write_text(
            json.dumps(marker_payload, indent=4, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        atomic_replace_dir(staging, directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return directory


def gitlinks(name: str, specification: dict, cache_root: Path) -> list[dict]:
    directory, commit, _ = fetch_commit(
        name, specification, cache_root, specification["commit"]
    )
    try:
        content = run_retry(
            ["git", "-C", str(directory), "show", f"{commit}:.gitmodules"]
        )
    except RuntimeError:
        return []
    config = configparser.ConfigParser()
    config.read_string(content)
    result: list[dict] = []
    for section in config.sections():
        path = config[section]["path"]
        entry = run_retry(["git", "-C", str(directory), "ls-tree", commit, path])
        if entry:
            result.append(
                {
                    "path": path,
                    "repository": config[section]["url"],
                    "commit": entry.split()[2],
                }
            )
    return result
