"""Generate structured release notes comparing current dependencies against a baseline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import ROOT


def _dep_ver(record: dict) -> str:
    return record.get("version") or (record.get("commit", "")[:12]) or "-"


def generate_release_notes(
    current: dict,
    sums_text: str,
    *,
    release: str,
    commit: str,
    baseline: dict | None = None,
    baseline_label: str | None = None,
) -> str:
    lines = [f"Release `{release}` (commit `{commit}`)", ""]
    if baseline is not None:
        old_deps = baseline.get("dependencies", {})
        new_deps = current.get("dependencies", {})
        changes: list[tuple[str, str, str]] = []
        for name in sorted(set(old_deps) | set(new_deps)):
            if name not in old_deps:
                changes.append((name, "（新增）", _dep_ver(new_deps[name])))
            elif name not in new_deps:
                changes.append((name, _dep_ver(old_deps[name]), "（移除）"))
            else:
                ov, nv = _dep_ver(old_deps[name]), _dep_ver(new_deps[name])
                oc, nc = old_deps[name].get("commit"), new_deps[name].get("commit")
                if ov != nv or oc != nc:
                    old_label = ov if ov != nv else f"{ov} ({(oc or '')[:8]})"
                    new_label = nv if ov != nv else f"{nv} ({(nc or '')[:8]})"
                    changes.append((name, old_label, new_label))
        old_tc = baseline.get("toolchains", {})
        new_tc = current.get("toolchains", {})
        for tc in sorted(set(old_tc) | set(new_tc)):
            ov = old_tc.get(tc, {}).get("version", "-")
            nv = new_tc.get(tc, {}).get("version", "-")
            if ov != nv:
                changes.append((f"toolchain:{tc}", ov, nv))
            elif tc == "android":
                o_rev = old_tc.get(tc, {}).get("revision", "-")
                n_rev = new_tc.get(tc, {}).get("revision", "-")
                if o_rev != n_rev:
                    changes.append((f"toolchain:{tc}", o_rev, n_rev))
            if tc == "linux":
                o_llvm = old_tc.get(tc, {}).get("llvm", "-")
                n_llvm = new_tc.get(tc, {}).get("llvm", "-")
                if o_llvm != n_llvm:
                    changes.append(("toolchain:linux:llvm", o_llvm, n_llvm))
        old_tools = baseline.get("buildTools", {})
        new_tools = current.get("buildTools", {})
        for tool in sorted(set(old_tools) | set(new_tools)):
            if tool not in old_tools:
                changes.append((f"build-tool:{tool}", "（新增）", _dep_ver(new_tools[tool])))
            elif tool not in new_tools:
                changes.append((f"build-tool:{tool}", _dep_ver(old_tools[tool]), "（移除）"))
            else:
                ov = _dep_ver(old_tools[tool])
                nv = _dep_ver(new_tools[tool])
                if ov != nv:
                    changes.append((f"build-tool:{tool}", ov, nv))
        lines.append(
            f"### 版本升级明细（对比 `{baseline_label}`）"
            if baseline_label
            else "### 版本升级明细"
        )
        lines.append("")
        if changes:
            lines += ["| 组件 | 旧版本 | 新版本 |", "| --- | --- | --- |"]
            for name, ov, nv in changes:
                lines.append(f"| {name} | {ov} | {nv} |")
        else:
            lines.append("本次依赖与工具链版本无变化（构建规则或配置更新）。")
        lines.append("")

    lines += ["### 当前核心依赖版本", "", "| 组件 | 版本 |", "| --- | --- |"]
    for name, rec in current.get("dependencies", {}).items():
        if "pinFrom" not in rec:
            lines.append(f"| {name} | {_dep_ver(rec)} |")
    lines += ["", "### SHA256SUMS", "", "```", sums_text.strip(), "```", ""]
    return "\n".join(lines)


def resolve_baseline_lock(
    baseline_ref: str | None = None, *, root: Path = ROOT
) -> tuple[dict | None, str | None]:
    candidates: list[str] = []
    if baseline_ref:
        candidates.append(baseline_ref)
    else:
        try:
            gh_res = subprocess.run(
                [
                    "gh",
                    "release",
                    "list",
                    "--limit",
                    "1",
                    "--json",
                    "tagName",
                    "-q",
                    ".[0].tagName",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            pass
        else:
            tag = gh_res.stdout.strip()
            if gh_res.returncode == 0 and tag:
                candidates.append(tag)
        candidates.append("HEAD~1")

    for ref in candidates:
        res = subprocess.run(
            ["git", "-C", str(root), "show", f"{ref}:dependencies.json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return json.loads(res.stdout), ref
    return None, None
