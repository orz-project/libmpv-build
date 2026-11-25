"""Transactional runtime packaging, dist cleanup, and SHA256SUMS generation."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import ROOT
from .config import (
    BUILD_CONFIG,
    RELEASE_RE,
    TARGETS,
    LockFile,
    TargetProfile,
    load_build_config,
    load_lock,
    load_profiles,
)
from .fs import atomic_write_text, sha256_file
from .locks import distribution_lock
from .recipes import dependency_identity, resolve_build_order
from .verify import verify_library_file


def _validate_loaded_build_config(root: Path) -> None:
    if load_build_config(root / "build-config.json") != BUILD_CONFIG:
        raise ValueError(
            "Build configuration changed after loading; start a new process before building or packaging"
        )


def _build_inputs_snapshot(root: Path) -> dict[str, str]:
    paths = [
        root / name
        for name in (
            "build-config.json",
            "dependencies.json",
            "targets.json",
            "Dockerfile",
            "LICENSE",
            "README.md",
        )
    ]
    for directory in ("libmpv_build", "patches", "profiles", "include/mpv"):
        base = root / directory
        if base.is_dir():
            paths.extend(base.rglob("*"))
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(paths)
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _expected_zip_name(release: str, target: str) -> str:
    return f"libmpv-{release.removeprefix('mpv-')}-{target}.zip"


def _installed_library(profile: TargetProfile, prefix: Path) -> Path:
    candidates = [prefix / "lib" / profile.library, prefix / "bin" / profile.library]
    if profile.name == "windows-x64":
        candidates += [prefix / "bin/mpv-2.dll", prefix / "lib/mpv-2.dll"]
    if profile.name.startswith("android-"):
        candidates += [prefix / "lib/libmpv.so.2"]
    installed = next((item for item in candidates if item.is_file()), None)
    if installed is None:
        raise ValueError(f"Installed libmpv runtime is missing under {prefix}")
    return installed


def _delivery_inputs_snapshot(profile: TargetProfile, prefix: Path) -> dict[str, str]:
    headers = sorted((prefix / "include/mpv").glob("*.h"))
    if not headers:
        raise ValueError(f"Installed libmpv public headers are missing under {prefix}")
    return {
        path.relative_to(prefix).as_posix(): sha256_file(path)
        for path in [_installed_library(profile, prefix), *headers]
    }


def clean_dist(
    output_dir: Path, release: str, targets: list[str] | tuple[str, ...]
) -> list[str]:
    """Remove stale releases and selected targets' previous zips while preserving other current targets."""
    selected = set(targets)
    if not selected or selected - set(TARGETS) or not RELEASE_RE.fullmatch(release):
        raise ValueError("Dist cleanup requires a valid release and known targets")
    output_dir.mkdir(parents=True, exist_ok=True)
    keep_names = {
        _expected_zip_name(release, target)
        for target in TARGETS
        if target not in selected
    }
    removed: list[str] = []
    with distribution_lock(output_dir):
        entries = sorted(output_dir.iterdir())
        remaining_zips = [
            p
            for p in entries
            if p.name in keep_names and p.is_file() and not p.is_symlink()
        ]
        sums_text = "".join(f"{sha256_file(p)}  {p.name}\n" for p in remaining_zips)
        keep_names = {p.name for p in remaining_zips} | {".lock"}
        with tempfile.TemporaryDirectory(
            prefix=".dist-clean-", dir=output_dir.parent
        ) as transaction:
            backup = Path(transaction)
            try:
                for entry in entries:
                    if entry.name in keep_names:
                        continue
                    entry.replace(backup / entry.name)
                    removed.append(entry.name)
                if remaining_zips:
                    atomic_write_text(output_dir / "SHA256SUMS", sums_text)
            except BaseException:
                for name in reversed(removed):
                    (backup / name).replace(output_dir / name)
                raise
    return removed


def create_deterministic_zip(directory: Path, output: Path, epoch: int) -> None:
    stamp = datetime.datetime.fromtimestamp(
        max(315532800, epoch), datetime.timezone.utc
    ).timetuple()[:6]
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if path.is_symlink():
                raise ValueError(f"Delivery must not contain a symbolic link: {path}")
            entry = zipfile.ZipInfo(path.relative_to(directory).as_posix(), stamp)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            with (
                path.open("rb") as incoming,
                target.open(entry, "w", force_zip64=True) as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing)


def promote_files(files: dict[str, Path], output: Path, immutable: set[str]) -> None:
    if (
        not files
        or immutable - files.keys()
        or any(
            Path(name).name != name or name in ("", ".", "..", ".lock", "SHA256SUMS")
            for name in files
        )
    ):
        raise ValueError("Artifact promotion requires flat, non-reserved file names")
    with (
        distribution_lock(output),
        tempfile.TemporaryDirectory(prefix=".transaction-", dir=output) as transaction,
    ):
        stage = Path(transaction) / "incoming"
        backup = Path(transaction) / "previous"
        for name, path in files.items():
            incoming = stage / name
            incoming.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, incoming)
        for name in immutable:
            existing = output / name
            if existing.is_symlink() or (
                existing.exists()
                and (
                    not existing.is_file()
                    or sha256_file(existing) != sha256_file(stage / name)
                )
            ):
                raise ValueError(
                    f"Immutable artifact differs from the previous release: {name}; select a new release identifier"
                )
        existing_zips = list(output.glob("*.zip"))
        if any(path.is_symlink() or not path.is_file() for path in existing_zips):
            raise ValueError("Existing delivery ZIPs must be regular files")
        sums = {path.name: sha256_file(path) for path in existing_zips}
        sums.update(
            {name: sha256_file(stage / name) for name in files if name.endswith(".zip")}
        )
        (stage / "SHA256SUMS").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
            encoding="utf-8",
        )
        names = [*files, "SHA256SUMS"]
        original: set[str] = set()
        changed: list[str] = []
        for name in names:
            existing = output / name
            if existing.is_file():
                previous = backup / name
                previous.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(existing, previous)
                original.add(name)
        try:
            for name in names:
                if name in immutable and name in original:
                    continue
                destination = output / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                (stage / name).replace(destination)
                changed.append(name)
        except BaseException:
            for name in reversed(changed):
                destination = output / name
                if name in original:
                    (backup / name).replace(destination)
                else:
                    destination.unlink(missing_ok=True)
            raise


def _validate_build_info(
    lock: LockFile, profile: TargetProfile, prefix: Path, root: Path = ROOT
) -> dict:
    _validate_loaded_build_config(root)
    info_path = prefix / "build-info.json"
    if not info_path.is_file():
        raise ValueError(f"Missing build-info.json under {prefix}")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if (
        not isinstance(info, dict)
        or info.get("target") != profile.name
        or info.get("release") != lock.release
    ):
        raise ValueError(
            "Compiled prefix has stale or foreign build inputs; rebuild this target before packaging"
        )
    if not isinstance(info.get("compiler"), str) or not info["compiler"]:
        raise ValueError("Compiled prefix build-info.json is missing compiler identity")
    dep_ids: dict[str, str] = {}
    for name in resolve_build_order(profile.name, lock, profile):
        dep_ids[name] = dependency_identity(
            name,
            lock=lock,
            profile=profile,
            compiler_version=info["compiler"],
            upstream_ids=dep_ids,
            root=root,
        )
    if (
        info.get("profile") != profile.to_dict()
        or info.get("depIdentities") != dep_ids
        or info.get("identity") != dep_ids["mpv"]
    ):
        raise ValueError(
            "Compiled prefix has stale build identity; rebuild this target before packaging"
        )
    if info.get("deliveryInputs") != _delivery_inputs_snapshot(profile, prefix):
        raise ValueError(
            "Compiled prefix delivery inputs differ from its build record; rebuild this target before packaging"
        )
    return info


def package_target(
    target: str,
    prefix: Path,
    output_dir: Path,
    strip_tool: str = "llvm-strip",
    *,
    run_smoke: bool = True,
    verify_headers: bool = True,
    root: Path = ROOT,
) -> dict:
    if not verify_headers or (target == "linux-x64" and not run_smoke):
        raise ValueError(
            "Header parity and Linux load/init smoke verification are mandatory for packaging"
        )
    input_snapshot = _build_inputs_snapshot(root)
    lock = load_lock(root / "dependencies.json")
    profile = load_profiles(root / "targets.json")[target]

    library_name = profile.library
    installed = _installed_library(profile, prefix)

    def prefix_snapshot() -> dict[str, str]:
        return {
            **_delivery_inputs_snapshot(profile, prefix),
            "build-info.json": sha256_file(prefix / "build-info.json"),
        }

    prefix_inputs = prefix_snapshot()
    _validate_build_info(lock, profile, prefix, root)
    with tempfile.TemporaryDirectory(
        prefix=".package-", dir=prefix.parent
    ) as temporary:
        stage = Path(temporary)
        runtime = stage / "runtime"
        runtime.mkdir(parents=True)
        library = runtime / library_name
        shutil.copyfile(installed, library)
        if (
            sha256_file(library)
            != prefix_inputs[installed.relative_to(prefix).as_posix()]
        ):
            raise ValueError(
                "Copied runtime differs from its build record; previous successful artifacts retained"
            )
        subprocess.run([strip_tool, "--strip-unneeded", str(library)], check=True)

        verification = verify_library_file(
            target,
            library,
            built_headers_dir=(prefix / "include/mpv") if verify_headers else None,
            repo_headers_dir=(root / "include/mpv") if verify_headers else None,
            run_smoke=run_smoke,
        )

        details = {
            "name": library_name,
            "bytes": library.stat().st_size,
            "sha256": sha256_file(library),
        }
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        archive_path = stage / _expected_zip_name(lock.release, target)
        create_deterministic_zip(runtime, archive_path, epoch)
        artifacts = {archive_path.name: archive_path}
        receipt = {
            "release": lock.release,
            "target": target,
            "library": details,
            "verification": verification,
            "artifacts": {
                archive_path.name: {
                    "sha256": sha256_file(archive_path),
                    "bytes": archive_path.stat().st_size,
                }
            },
        }
        current_prefix = prefix_snapshot()
        if (
            input_snapshot != _build_inputs_snapshot(root)
            or prefix_inputs != current_prefix
        ):
            raise ValueError(
                "Build or delivery inputs changed during packaging; previous successful artifacts retained"
            )
        promote_files(artifacts, output_dir, set(artifacts))
        return receipt
