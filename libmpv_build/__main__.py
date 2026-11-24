"""Module entrypoint for `python3 -m libmpv_build`."""

from __future__ import annotations

from .cli import entrypoint

if __name__ == "__main__":
    raise SystemExit(entrypoint())
