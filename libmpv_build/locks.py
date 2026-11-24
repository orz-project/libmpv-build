"""Advisory flock helpers for cache, target builds, and dist promotion."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def file_lock(
    path: Path, *, non_blocking: bool = False, busy_message: str | None = None
) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, mode)
        except BlockingIOError:
            raise RuntimeError(
                busy_message or f"Lock is currently held: {path}"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def cache_lock(cache_root: Path, namespace: str, key: str):
    digest = hashlib.sha256((namespace + ":" + key).encode("utf-8")).hexdigest()
    return file_lock(cache_root / "locks" / f"{digest}.lock")


def target_lock(build_root: Path, target: str):
    return file_lock(
        build_root / ".locks" / f"{target}.lock",
        non_blocking=True,
        busy_message=f"Another process is building {target}",
    )


def distribution_lock(dist_dir: Path):
    return file_lock(dist_dir / ".lock")
