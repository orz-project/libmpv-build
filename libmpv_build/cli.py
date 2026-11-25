"""Unified CLI entrypoint for libmpv_build."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from . import ROOT
from .builder import build_container_image, build_targets
from .config import TARGETS, load_lock
from .locks import target_lock
from .package import package_target
from .release_notes import generate_release_notes, resolve_baseline_lock
from .update import run_update
from .verify import VerificationError, verify_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m libmpv_build", description="libmpv build system"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_p = subparsers.add_parser("build", help="Build locked libmpv runtimes")
    selection = build_p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--target", choices=TARGETS)
    selection.add_argument("--all", action="store_true")
    build_p.add_argument(
        "--jobs", type=int, default=max(1, min(os.cpu_count() or 1, 8))
    )
    build_p.add_argument("--ndk", help="Exact locked Android NDK directory")
    build_p.add_argument("--toolchain", help="Exact locked llvm-mingw UCRT directory")
    build_p.add_argument(
        "--container",
        action="store_true",
        help="Run inside pinned Ubuntu 22.04 Docker container",
    )
    build_p.add_argument(
        "--reuse-container-image",
        action="store_true",
        help="Use an already prepared image that matches the current lock",
    )
    build_p.add_argument(
        "--verify-cache",
        action="store_true",
        help="Perform strict byte-for-byte cache SHA-256 checks",
    )
    build_p.add_argument(
        "--no-clean-dist", action="store_true", help="Skip dist cleanup before building"
    )

    subparsers.add_parser(
        "container-image", help="Build the local image for the current toolchain lock"
    )

    pkg_p = subparsers.add_parser(
        "package", help="Verify and package an already-built target prefix"
    )
    pkg_p.add_argument("--target", choices=TARGETS, required=True)
    pkg_p.add_argument("--prefix", type=Path, required=True)
    pkg_p.add_argument("--output", type=Path, default=ROOT / "dist")
    pkg_p.add_argument("--strip", default="llvm-strip")

    ver_p = subparsers.add_parser(
        "verify", help="Verify binary ABI, alignment, imports, exports, and headers"
    )
    ver_p.add_argument("--target", choices=TARGETS, required=True)
    ver_p.add_argument(
        "--path", type=Path, required=True, help="Path to built library or delivery ZIP"
    )
    ver_p.add_argument(
        "--headers-dir",
        type=Path,
        help="Optional built include/mpv directory to compare",
    )
    ver_p.add_argument(
        "--smoke", action="store_true", help="Run Linux ctypes load/init smoke test"
    )

    upd_p = subparsers.add_parser(
        "update", help="Resolve upstream stable tags and update dependencies.json"
    )
    upd_p.add_argument("--lock", type=Path, default=ROOT / "dependencies.json")
    upd_p.add_argument("--cache", type=Path, default=ROOT / ".cache")
    upd_p.add_argument("--write", action="store_true")
    upd_p.add_argument("--commit", action="store_true")
    upd_p.add_argument("--github-output", type=Path)

    notes_p = subparsers.add_parser(
        "release-notes", help="Generate markdown release notes"
    )
    notes_p.add_argument("--lock", type=Path, default=ROOT / "dependencies.json")
    notes_p.add_argument("--sums", type=Path, default=ROOT / "dist/SHA256SUMS")
    notes_p.add_argument(
        "--release", help="Release identifier (defaults to lock release)"
    )
    notes_p.add_argument("--commit", default="HEAD")
    notes_p.add_argument(
        "--baseline-ref", help="Explicit git ref for baseline comparison"
    )
    notes_p.add_argument("--output", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "build":
        if args.jobs < 1:
            parser.error("--jobs must be positive")
        if args.reuse_container_image and not args.container:
            parser.error("--reuse-container-image requires --container")
        if platform.system() != "Linux":
            parser.error("Use a Linux build host (including a Linux container runner)")
        lock = load_lock(ROOT / "dependencies.json")
        targets = list(TARGETS) if args.all else [args.target]
        receipts = build_targets(
            lock,
            targets,
            jobs=args.jobs,
            ndk=args.ndk,
            toolchain=args.toolchain,
            container=args.container,
            reuse_container_image=args.reuse_container_image,
            verify_cache=args.verify_cache,
            clean_dist_flag=not args.no_clean_dist,
        )
        if receipts:
            print(json.dumps(receipts, indent=4))
        return 0

    if args.command == "container-image":
        lock = load_lock(ROOT / "dependencies.json")
        print(build_container_image(lock))
        return 0

    if args.command == "package":
        with target_lock(ROOT / "build", args.target):
            receipt = package_target(
                args.target,
                args.prefix,
                args.output,
                args.strip,
            )
        print(json.dumps(receipt, indent=4))
        return 0

    if args.command == "verify":
        report = verify_path(
            args.target,
            args.path,
            built_headers_dir=args.headers_dir,
            repo_headers_dir=(ROOT / "include/mpv") if args.headers_dir else None,
            run_smoke=args.smoke,
        )
        print(json.dumps(report, indent=4))
        return 0

    if args.command == "update":
        result = run_update(
            lock_path=args.lock,
            cache_root=args.cache,
            write=args.write,
            commit=args.commit,
            github_output=args.github_output,
        )
        print(json.dumps(result, indent=4))
        return 0

    if args.command == "release-notes":
        current = json.loads(args.lock.read_text(encoding="utf-8"))
        sums_text = args.sums.read_text(encoding="utf-8") if args.sums.is_file() else ""
        baseline, label = resolve_baseline_lock(args.baseline_ref)
        notes = generate_release_notes(
            current,
            sums_text,
            release=args.release or current["release"],
            commit=args.commit,
            baseline=baseline,
            baseline_label=label,
        )
        if args.output:
            args.output.write_text(notes, encoding="utf-8")
        else:
            print(notes)
        return 0

    return 1


def entrypoint() -> int:
    try:
        return main()
    except (
        OSError,
        ValueError,
        RuntimeError,
        VerificationError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
