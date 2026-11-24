"""File hashing, atomic replacements, safe archive extraction, and tree manifest verification."""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_replace_dir(staging: Path, destination: Path) -> None:
    """Promote a staged tree, restoring the previous tree if promotion fails."""
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError(f"Staging directory is invalid: {staging}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_root = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}-previous-", dir=destination.parent
        )
    )
    previous = backup_root / "tree"
    promoted = False
    try:
        if destination.exists() or destination.is_symlink():
            destination.replace(previous)
        try:
            staging.replace(destination)
            promoted = True
        except BaseException:
            if previous.exists() or previous.is_symlink():
                previous.replace(destination)
            raise
    finally:
        if promoted or not (previous.exists() or previous.is_symlink()):
            shutil.rmtree(backup_root)


def _normalize_member_path(
    name: str, *, strip_prefix: str | None = None
) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or ":" in name
    ):
        raise ValueError(f"Unsafe archive path: {name}")
    if strip_prefix is not None:
        if path.parts[0] != strip_prefix:
            raise ValueError(f"Unexpected archive prefix in member: {name}")
        path = PurePosixPath(*path.parts[1:])
        if not path.parts:
            return None
    posix = path.as_posix()
    if posix in (".complete", ".complete.json", ".libmpv-source.json"):
        raise ValueError(f"Reserved archive path: {name}")
    return path


def _validate_symlink_target(member_path: PurePosixPath, linkname: str) -> None:
    target = PurePosixPath(linkname)
    resolved = posixpath.normpath((member_path.parent / target).as_posix())
    if (
        target.is_absolute()
        or resolved == ".."
        or resolved.startswith("../")
        or "\\" in linkname
        or ":" in linkname
    ):
        raise ValueError(
            f"Unsafe archive symlink: {member_path.as_posix()} -> {linkname}"
        )


def _iter_archive_entries(archive: Path, *, strip_prefix: str | None = None):
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                rel = _normalize_member_path(member.filename, strip_prefix=strip_prefix)
                if rel is None:
                    if not member.is_dir():
                        raise ValueError(
                            f"Archive root must be a directory: {member.filename}"
                        )
                    continue
                mode = member.external_attr >> 16
                if member.is_dir():
                    yield rel, "directory", (mode & 0o777) or 0o755, None, None
                elif stat.S_ISLNK(mode):
                    yield rel, "symlink", 0, source.read(member).decode("utf-8"), None
                elif stat.S_IFMT(mode) in (0, stat.S_IFREG):
                    with source.open(member) as incoming:
                        yield rel, "file", (mode & 0o777) or 0o644, None, incoming
                else:
                    raise ValueError(f"Unsupported ZIP member: {member.filename}")
    else:
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                rel = _normalize_member_path(member.name, strip_prefix=strip_prefix)
                if rel is None:
                    if not member.isdir():
                        raise ValueError(
                            f"Archive root must be a directory: {member.name}"
                        )
                    continue
                if member.isdir():
                    yield rel, "directory", member.mode & 0o777, None, None
                elif member.isfile():
                    with source.extractfile(member) as incoming:
                        yield rel, "file", member.mode & 0o777, None, incoming
                elif member.issym():
                    yield rel, "symlink", 0, member.linkname, None
                elif member.islnk():
                    link_rel = _normalize_member_path(
                        member.linkname, strip_prefix=strip_prefix
                    )
                    if link_rel is None:
                        raise ValueError(f"Invalid hardlink target: {member.linkname}")
                    yield rel, "hardlink", 0, link_rel.as_posix(), None
                else:
                    raise ValueError(f"Unsupported TAR member: {member.name}")


def archive_hash_tree(
    archive: Path, *, strip_prefix: str | None = None
) -> dict[str, dict]:
    """Derive exact content hashes and permissions from a verified archive."""
    tree: dict[str, dict] = {}
    hardlinks: dict[str, str] = {}
    for rel, kind, mode, link, stream in _iter_archive_entries(
        archive, strip_prefix=strip_prefix
    ):
        posix = rel.as_posix()
        if posix in tree and (
            kind != "directory" or tree[posix]["type"] != "directory"
        ):
            raise ValueError(f"Duplicate archive member: {posix}")
        for parent in rel.parents:
            if parent.parts:
                tree.setdefault(parent.as_posix(), {"type": "directory", "mode": 0o755})
        if kind == "directory":
            tree[posix] = {"type": "directory", "mode": mode}
        elif kind == "file":
            hasher = hashlib.sha256()
            while block := stream.read(1024 * 1024):
                hasher.update(block)
            tree[posix] = {"type": "file", "mode": mode, "sha256": hasher.hexdigest()}
        elif kind == "symlink":
            _validate_symlink_target(rel, link)
            tree[posix] = {"type": "symlink", "target": link}
        elif kind == "hardlink":
            hardlinks[posix] = link
            tree[posix] = {"type": "hardlink"}

    def resolve_hardlink(name: str, seen: tuple[str, ...] = ()) -> dict:
        if name in seen or name not in tree:
            raise ValueError(f"Invalid archive hardlink: {name}")
        rec = tree[name]
        if rec["type"] == "hardlink":
            rec = resolve_hardlink(hardlinks[name], (*seen, name))
            if rec["type"] != "file":
                raise ValueError(f"Hardlink must target a regular file: {name}")
            tree[name] = rec
        return rec

    for name in hardlinks:
        resolve_hardlink(name)

    for name in tree:
        if any(
            tree[parent.as_posix()]["type"] != "directory"
            for parent in PurePosixPath(name).parents
            if parent.parts
        ):
            raise ValueError(f"Archive member nested below non-directory: {name}")
    return tree


def extract_archive_safe(
    archive: Path, destination: Path, *, strip_prefix: str | None = None
) -> None:
    """Extract regular files first, then hardlinks, then validated symlinks inside destination."""
    if destination.is_symlink() or (
        destination.exists()
        and (not destination.is_dir() or any(destination.iterdir()))
    ):
        raise ValueError(
            f"Extraction destination must be an empty directory: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    symlinks: list[tuple[PurePosixPath, Path, str]] = []
    hardlinks: list[tuple[Path, Path]] = []
    members: dict[str, str] = {}
    directory_modes: dict[Path, int] = {}
    dest_resolved = destination.resolve()
    for rel, kind, mode, link, stream in _iter_archive_entries(
        archive, strip_prefix=strip_prefix
    ):
        name = rel.as_posix()
        if name in members and (kind != "directory" or members[name] != "directory"):
            raise ValueError(f"Duplicate archive member: {name}")
        for parent in rel.parents:
            if parent.parts:
                if members.get(parent.as_posix(), "directory") != "directory":
                    raise ValueError(
                        f"Archive member nested below non-directory: {name}"
                    )
                members.setdefault(parent.as_posix(), "directory")
                directory_modes.setdefault(destination.joinpath(*parent.parts), 0o755)
        members[name] = kind
        target_path = destination.joinpath(*rel.parts)
        if kind == "directory":
            target_path.mkdir(parents=True, exist_ok=True)
            directory_modes[target_path] = mode
        elif kind == "file":
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with target_path.open("wb") as outgoing:
                shutil.copyfileobj(stream, outgoing)
            target_path.chmod(mode)
        elif kind == "symlink":
            _validate_symlink_target(rel, link)
            symlinks.append((rel, target_path, link))
        elif kind == "hardlink":
            link_path = destination.joinpath(*PurePosixPath(link).parts)
            hardlinks.append((target_path, link_path))

    while hardlinks:
        waiting: list[tuple[Path, Path]] = []
        for path, target in hardlinks:
            if target.is_file() and not target.is_symlink():
                path.parent.mkdir(parents=True, exist_ok=True)
                os.link(target, path)
            else:
                waiting.append((path, target))
        if len(waiting) == len(hardlinks):
            raise ValueError("Archive hardlinks have unavailable or cyclic targets")
        hardlinks = waiting

    for _, path, link in symlinks:
        if not (path.parent / link).resolve().is_relative_to(dest_resolved):
            raise ValueError(f"Unsafe archive symlink: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(link)

    for _, path, _ in symlinks:
        if not path.resolve().is_relative_to(dest_resolved):
            raise ValueError(f"Archive symlink escapes root: {path}")

    for path, mode in sorted(
        directory_modes.items(), key=lambda item: len(item[0].parts), reverse=True
    ):
        path.chmod(mode)


def capture_tree_manifest(
    directory: Path, *, exclude: tuple[str, ...] = ()
) -> dict[str, dict]:
    """Capture fast stat/permission/symlink manifest without following directory symlinks."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"Cannot capture manifest for non-directory: {directory}")
    excluded = set(exclude)
    root_resolved = directory.resolve()
    manifest: dict[str, dict] = {}
    for base, directories, files in os.walk(directory, followlinks=False):
        for name in (*directories, *files):
            path = Path(base) / name
            relative = path.relative_to(directory).as_posix()
            if relative in excluded:
                continue
            st = path.lstat()
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                if not path.resolve().is_relative_to(root_resolved):
                    raise ValueError(f"Escaping symlink in tree: {relative}")
                manifest[relative] = {"type": "symlink", "target": os.readlink(path)}
            elif stat.S_ISREG(mode):
                manifest[relative] = {
                    "type": "file",
                    "mode": mode & 0o777,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                }
            elif stat.S_ISDIR(mode):
                manifest[relative] = {"type": "directory", "mode": mode & 0o777}
            else:
                raise ValueError(f"Unsupported file type in tree: {relative}")
    return manifest


def verify_tree_manifest(
    directory: Path,
    expected_manifest: dict[str, dict],
    *,
    exclude: tuple[str, ...] = (),
    strict_hashes: dict[str, dict] | None = None,
) -> bool:
    """Verify directory against stat manifest, and optionally full SHA-256 hashes."""
    try:
        actual = capture_tree_manifest(directory, exclude=exclude)
    except (OSError, ValueError, RuntimeError):
        return False
    if actual != expected_manifest:
        return False
    if strict_hashes is not None:
        if set(strict_hashes) != set(actual):
            return False
        for rel, spec in strict_hashes.items():
            entry = actual[rel]
            if entry["type"] != spec["type"]:
                return False
            if spec["type"] == "file":
                if (
                    entry["mode"] != spec["mode"]
                    or sha256_file(directory / rel) != spec["sha256"]
                ):
                    return False
            elif spec["type"] == "symlink":
                if entry["target"] != spec["target"]:
                    return False
            elif (
                spec["type"] == "directory"
                and "mode" in spec
                and entry["mode"] != spec["mode"]
            ):
                return False
    return True
