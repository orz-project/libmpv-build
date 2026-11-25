"""Incremental per-dependency DAG builder and container orchestrator."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ROOT
from .config import (
    BUILD_CONFIG,
    STATIC_PACKAGES,
    TARGETS,
    LockFile,
    TargetProfile,
    load_profiles,
)
from .fs import (
    atomic_replace_dir,
    atomic_write_text,
    capture_tree_manifest,
    sha256_file,
    verify_tree_manifest,
)
from .locks import target_lock
from .package import (
    _build_inputs_snapshot,
    _delivery_inputs_snapshot,
    _validate_loaded_build_config,
    clean_dist,
    package_target,
)
from .patching import enforce_static_meson_dependencies
from .process import capture_cmd, require_executable, run_cmd
from .recipes import RECIPES, BuildContext, dependency_identity, resolve_build_order
from .runners import write_toolchain_files
from .sources import materialize_source_tree
from .toolchains import ToolchainContext, prepare_toolchain

STAGE_COMPLETE = ".complete.json"


def _stage_is_valid(
    stage: Path, dependency: str, identity: str, *, strict: bool
) -> bool:
    marker = stage / STAGE_COMPLETE
    if stage.is_symlink() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        if (
            not isinstance(info, dict)
            or info.get("identity") != identity
            or info.get("dependency") != dependency
        ):
            return False
        manifest = info.get("manifest")
        if not isinstance(manifest, dict) or not verify_tree_manifest(
            stage, manifest, exclude=(STAGE_COMPLETE,)
        ):
            return False
        if strict:
            hashes = info.get("hashes")
            if not isinstance(hashes, dict):
                return False
            current = {
                path: sha256_file(stage / path)
                for path, record in manifest.items()
                if record["type"] == "file"
            }
            return hashes == current
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def assemble_prefix(stage_dirs: list[Path], prefix_dir: Path) -> None:
    """Materialize a clean prefix from immutable dependency stages without sharing mutable .pc files."""
    prefix_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".prefix-", dir=prefix_dir.parent))
    try:
        (staging / "lib").mkdir(parents=True, exist_ok=True)
        (staging / "include").mkdir(parents=True, exist_ok=True)
        for stage in stage_dirs:
            if stage.is_symlink() or not stage.is_dir():
                raise ValueError(f"Dependency stage is missing: {stage}")
            stage_root = stage.resolve()
            for source_path in sorted(stage.rglob("*")):
                rel = source_path.relative_to(stage)
                dest_path = staging / rel
                for parent in reversed(rel.parents):
                    parent_path = staging / parent
                    if parent_path.is_symlink() or (
                        parent_path.exists() and not parent_path.is_dir()
                    ):
                        raise ValueError(
                            f"Dependency stage conflicts with a non-directory prefix parent: {rel}"
                        )
                    parent_path.mkdir(exist_ok=True)
                if source_path.is_symlink():
                    link = source_path.readlink()
                    resolved = source_path.resolve()
                    if not resolved.is_relative_to(stage_root):
                        raise ValueError(
                            f"Dependency stage symlink escapes its root: {source_path}"
                        )
                    if link.is_absolute():
                        link = Path(
                            os.path.relpath(resolved, source_path.parent.resolve())
                        )
                    if dest_path.exists() or dest_path.is_symlink():
                        if dest_path.is_dir() and not dest_path.is_symlink():
                            raise ValueError(
                                f"Dependency stage conflicts with a prefix directory: {rel}"
                            )
                        dest_path.unlink()
                    dest_path.symlink_to(link)
                elif source_path.is_dir():
                    if dest_path.is_symlink() or (
                        dest_path.exists() and not dest_path.is_dir()
                    ):
                        raise ValueError(
                            f"Dependency stage conflicts with a non-directory prefix entry: {rel}"
                        )
                    dest_path.mkdir(exist_ok=True)
                elif source_path.is_file():
                    if rel.as_posix() == STAGE_COMPLETE:
                        continue
                    if dest_path.is_symlink():
                        dest_path.unlink()
                    elif dest_path.exists() and not dest_path.is_file():
                        raise ValueError(
                            f"Dependency stage conflicts with a prefix directory: {rel}"
                        )
                    shutil.copy2(source_path, dest_path)
                else:
                    raise ValueError(
                        f"Unsupported dependency stage entry: {source_path}"
                    )
        atomic_replace_dir(staging, prefix_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _prune_stale_stages(dep_stages_dir: Path, keep_id: str) -> None:
    if not dep_stages_dir.is_dir():
        return
    for child in dep_stages_dir.iterdir():
        if child.name == keep_id:
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def _transitive_deps(name: str, system: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(item: str) -> None:
        for dep in RECIPES[item].deps_for(system):
            if dep not in seen:
                visit(dep)
                seen.add(dep)
                ordered.append(dep)

    visit(name)
    return ordered


class TargetBuilder:
    def __init__(
        self,
        lock: LockFile,
        profile: TargetProfile,
        *,
        jobs: int,
        ndk: str | None = None,
        toolchain: str | None = None,
        verify_cache: bool = False,
        root: Path = ROOT,
    ) -> None:
        self.lock = lock
        self.profile = profile
        self.jobs = jobs
        self.ndk = ndk
        self.toolchain = toolchain
        self.verify_cache = verify_cache
        self.root = root
        self.base = root / "build" / profile.name
        self.prefix = self.base / "prefix"
        self.stages_root = self.base / "stages"
        self.cache_root = root / ".cache"

    def build(self) -> dict:
        _validate_loaded_build_config(self.root)
        input_snapshot = _build_inputs_snapshot(self.root)
        expected_meson = self.lock.build_tools.get("meson", {}).get("version")
        if expected_meson and capture_cmd(["meson", "--version"]) != expected_meson:
            raise RuntimeError("Meson must match the locked stable build-tool version")

        self.base.mkdir(parents=True, exist_ok=True)
        tc: ToolchainContext = prepare_toolchain(
            self.lock,
            self.profile,
            self.prefix,
            ndk=self.ndk,
            toolchain=self.toolchain,
            cache_root=self.cache_root,
            verify_cache=self.verify_cache,
        )
        write_toolchain_files(tc, self.base)

        order = resolve_build_order(self.profile.name, self.lock, self.profile)
        dep_ids: dict[str, str] = {}
        stage_dirs: dict[str, Path] = {}

        def prepare_source(dep_name: str) -> Path:
            destination = self.base / "sources" / dep_name
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or destination.is_file():
                    destination.unlink()
                else:
                    shutil.rmtree(destination)
            materialize_source_tree(
                self.lock.raw,
                dep_name,
                self.cache_root,
                destination,
                verify_cache=self.verify_cache,
            )
            enforce_static_meson_dependencies(destination, STATIC_PACKAGES)
            return destination

        def build_dir_for(dep_name: str) -> Path:
            path = self.base / "builddirs" / dep_name
            if path.exists():
                shutil.rmtree(path)
            return path

        ctx = BuildContext(
            lock=self.lock,
            tc=tc,
            base_dir=self.base,
            jobs=self.jobs,
            prepare_source=prepare_source,
            build_dir_for=build_dir_for,
        )

        for name in order:
            dep_id = dependency_identity(
                name,
                lock=self.lock,
                profile=self.profile,
                compiler_version=tc.compiler_version,
                upstream_ids=dep_ids,
                root=self.root,
            )
            dep_ids[name] = dep_id
            stage_dir = self.stages_root / name / dep_id
            stage_dirs[name] = stage_dir

            if _stage_is_valid(stage_dir, name, dep_id, strict=self.verify_cache):
                print(
                    f"[{self.profile.name}] cached {name} ({dep_id[:12]})", flush=True
                )
                continue

            upstream_names = _transitive_deps(name, self.profile.system)
            assemble_prefix([stage_dirs[dep] for dep in upstream_names], self.prefix)
            print(f"[{self.profile.name}] build {name} ({dep_id[:12]})", flush=True)

            stage_dir.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f".{name}-stage-", dir=stage_dir.parent
            ) as tmp_stage_str:
                tmp_stage = Path(tmp_stage_str)
                destdir = tmp_stage / "destdir"
                stage_prefix = tmp_stage / "prefix"
                destdir.mkdir(parents=True, exist_ok=True)
                stage_prefix.mkdir(parents=True, exist_ok=True)

                RECIPES[name].build(ctx, destdir, stage_prefix)
                manifest = capture_tree_manifest(
                    stage_prefix, exclude=(STAGE_COMPLETE,)
                )
                if not any(record["type"] == "file" for record in manifest.values()):
                    raise RuntimeError(
                        f"Dependency {name} installed no files into its isolated stage"
                    )
                atomic_write_text(
                    stage_prefix / STAGE_COMPLETE,
                    json.dumps(
                        {
                            "dependency": name,
                            "identity": dep_id,
                            "commit": self.lock.dependencies[name].commit
                            or self.lock.dependencies[name].sha256,
                            "manifest": manifest,
                            "hashes": {
                                path: sha256_file(stage_prefix / path)
                                for path, record in manifest.items()
                                if record["type"] == "file"
                            },
                        },
                        indent=4,
                    )
                    + "\n",
                )
                atomic_replace_dir(stage_prefix, stage_dir)
            _prune_stale_stages(self.stages_root / name, dep_id)

        assemble_prefix([stage_dirs[dep] for dep in order], self.prefix)
        if (
            input_snapshot != _build_inputs_snapshot(self.root)
            or json.loads((self.root / "dependencies.json").read_text(encoding="utf-8"))
            != self.lock.raw
            or load_profiles(self.root / "targets.json")[self.profile.name]
            != self.profile
        ):
            raise RuntimeError(
                "Build inputs changed during compilation; restart this target before packaging"
            )

        opt_tiers: dict[str, list[str]] = {}
        for dep_name, level in sorted(BUILD_CONFIG.optimization_tiers.items()):
            opt_tiers.setdefault(level, []).append(dep_name)
        metadata = {
            "release": self.lock.release,
            "target": self.profile.name,
            "identity": dep_ids["mpv"],
            "compiler": tc.compiler_version,
            "profile": self.profile.to_dict(),
            "optimization": {
                **opt_tiers,
                "lto": BUILD_CONFIG.toolchain.lto,
            },
            "depIdentities": dep_ids,
            "deliveryInputs": _delivery_inputs_snapshot(self.profile, self.prefix),
            "buildTools": {
                "meson": capture_cmd(["meson", "--version"]),
                "cmake": capture_cmd(["cmake", "--version"]).splitlines()[0],
                "autoconf": capture_cmd(["autoconf", "--version"]).splitlines()[0],
                "ninja": capture_cmd(["ninja", "--version"]),
            },
        }
        atomic_write_text(
            self.prefix / "build-info.json", json.dumps(metadata, indent=4) + "\n"
        )
        return package_target(
            self.profile.name,
            self.prefix,
            self.root / "dist",
            tc.strip,
            run_smoke=True,
            verify_headers=True,
            root=self.root,
        )


def container_image_identity(lock: LockFile, root: Path = ROOT) -> tuple[str, str]:
    linux = lock.toolchains["linux"].raw
    identity = hashlib.sha256(
        (root / "Dockerfile").read_bytes()
        + (root / "dependencies.json").read_bytes()
        + linux["image"].encode("utf-8")
    ).hexdigest()
    return identity, f"libmpv-build:{identity[:16]}"


def build_container_image(lock: LockFile, *, root: Path = ROOT) -> str:
    """Build and tag the container matching the current toolchain lock."""
    require_executable("docker")
    identity, image = container_image_identity(lock, root)
    linux = lock.toolchains["linux"].raw
    run_cmd(
        [
            "docker",
            "build",
            "--build-arg",
            f"UBUNTU_IMAGE={linux['image']}",
            "--label",
            f"org.libmpv-build.identity={identity}",
            "-t",
            image,
            ".",
        ],
        cwd=root,
    )
    return image


def _require_matching_container_image(
    lock: LockFile, *, root: Path = ROOT
) -> str:
    identity, image = container_image_identity(lock, root)
    try:
        actual = capture_cmd(
            [
                "docker",
                "image",
                "inspect",
                "--format={{ index .Config.Labels \"org.libmpv-build.identity\" }}",
                image,
            ],
            cwd=root,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Matching container image {image} is not available; create it with "
            "`python3 -m libmpv_build container-image` first"
        ) from error
    if actual != identity:
        raise RuntimeError(
            f"Container image {image} does not match the current lock; recreate it with "
            "`python3 -m libmpv_build container-image`"
        )
    return image


def run_in_container(
    lock: LockFile,
    targets: list[str],
    *,
    build_all: bool,
    jobs: int,
    ndk: str | None,
    toolchain: str | None,
    verify_cache: bool,
    no_clean_dist: bool,
    reuse_image: bool = False,
    root: Path = ROOT,
) -> None:
    require_executable("docker")
    image = (
        _require_matching_container_image(lock, root=root)
        if reuse_image
        else build_container_image(lock, root=root)
    )

    import os

    selection = ["--all"] if build_all else ["--target", targets[0]]
    command: list[str] = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{root}:/workspace",
    ]
    extra_args: list[str] = [*selection, "--jobs", str(jobs)]
    if verify_cache:
        extra_args.append("--verify-cache")
    if no_clean_dist:
        extra_args.append("--no-clean-dist")
    for value, option in ((ndk, "--ndk"), (toolchain, "--toolchain")):
        if value:
            mount = str(Path(value).expanduser().resolve())
            command += ["-v", f"{mount}:{mount}:ro"]
            extra_args += [option, mount]
    command += [image, *extra_args]
    run_cmd(command, cwd=root)


def build_targets(
    lock: LockFile,
    targets: list[str],
    *,
    jobs: int,
    ndk: str | None = None,
    toolchain: str | None = None,
    container: bool = False,
    reuse_container_image: bool = False,
    verify_cache: bool = False,
    clean_dist_flag: bool = True,
    root: Path = ROOT,
) -> list[dict]:
    if container:
        run_in_container(
            lock,
            targets,
            build_all=(len(targets) == len(TARGETS)),
            jobs=jobs,
            ndk=ndk,
            toolchain=toolchain,
            verify_cache=verify_cache,
            no_clean_dist=(not clean_dist_flag),
            reuse_image=reuse_container_image,
            root=root,
        )
        return []
    if reuse_container_image:
        raise ValueError("--reuse-container-image requires --container")

    profiles = load_profiles(root / "targets.json")
    receipts: list[dict] = []
    with contextlib.ExitStack() as stack:
        for target in sorted(set(targets)):
            stack.enter_context(target_lock(root / "build", target))
        if clean_dist_flag:
            clean_dist(root / "dist", lock.release, targets)
        for target in targets:
            receipts.append(
                TargetBuilder(
                    lock,
                    profiles[target],
                    jobs=jobs,
                    ndk=ndk,
                    toolchain=toolchain,
                    verify_cache=verify_cache,
                    root=root,
                ).build()
            )
    return receipts
