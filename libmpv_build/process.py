"""Subprocess execution with locale-safe capture and retry support."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import ROOT


def _c_locale_env(env: dict[str, str] | None) -> dict[str, str]:
    merged = (os.environ if env is None else env).copy()
    merged["LC_ALL"] = "C"
    merged.setdefault("LANG", "C")
    return merged


def run_cmd(
    arguments: list[str | Path], *, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> None:
    command = [str(item) for item in arguments]
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def capture_cmd(
    arguments: list[str | Path], *, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> str:
    command = [str(item) for item in arguments]
    return subprocess.check_output(
        command, cwd=cwd, env=_c_locale_env(env), text=True
    ).strip()


def run_retry(
    arguments: list[str | Path],
    *,
    cwd: Path | None = None,
    attempts: int = 3,
    timeout: int = 240,
) -> str:
    command = [str(item) for item in arguments]
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(attempts):
        try:
            return subprocess.check_output(
                command,
                cwd=cwd,
                env=_c_locale_env(None),
                text=True,
                stderr=subprocess.PIPE,
                timeout=timeout,
            ).strip()
        except subprocess.CalledProcessError as error:
            last_error = error
            if attempt == attempts - 1:
                raise RuntimeError(
                    f"{' '.join(command[:4])}: {error.stderr.strip()}"
                ) from error
            time.sleep(attempt + 1)
    raise RuntimeError(str(last_error))


def require_executable(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(
            f"Required build tool is missing: {name}; use --container or install the Dockerfile tool set"
        )
    return resolved
