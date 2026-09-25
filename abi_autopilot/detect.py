# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
"""Detect the target repository's stack and propose checks for `init`.

Looks at the repository root and its first-level folders for Node.js
(package.json), Python (pyproject.toml, setup.py, requirements.txt…), Go
(go.mod) and Rust (Cargo.toml) projects, and builds `workspace_bootstrap` and
`validation` entries from what each project actually declares (for Node, its
package.json scripts). The result is a starting point the operator reviews.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    "node_modules", "venv", "env", "dist", "build", "target", "vendor", "out",
    "coverage", "__pycache__", "site-packages",
}
PHASES = ("targeted", "fast", "full", "batch_fast", "batch_full")
NPM_PLACEHOLDER_TEST = "no test specified"


@dataclass
class Detection:
    bootstrap: list[dict] = field(default_factory=list)
    validation: dict[str, list[dict]] = field(default_factory=lambda: {p: [] for p in PHASES})
    generated_artifacts: list[str] = field(default_factory=list)
    done_artifacts: list[str] = field(default_factory=list)
    toolchain: dict = field(default_factory=dict)
    summary: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return any(self.validation[p] for p in PHASES)

    def add(self, phases: tuple[str, ...], check: dict):
        for phase in phases:
            self.validation[phase].append(dict(check))

    def artifacts(self, rels: list[str], done_only: list[str] = ()):
        for rel in rels:
            if rel not in self.generated_artifacts:
                self.generated_artifacts.append(rel)
        for rel in list(rels) + list(done_only):
            if rel not in self.done_artifacts:
                self.done_artifacts.append(rel)


def _rel(cwd: str, name: str) -> str:
    return f"{cwd}/{name}" if cwd else name


def _when(cwd: str) -> str:
    return f"changed:{cwd}/" if cwd else "always"


def _prefix(cwd: str) -> str:
    return f"{cwd.replace('/', '-')}-" if cwd else ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _candidate_dirs(repo: Path) -> list[tuple[str, Path]]:
    dirs = [("", repo)]
    try:
        children = sorted(repo.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.is_dir() and not child.name.startswith(".") and child.name not in SKIP_DIRS:
            dirs.append((child.name, child))
    return dirs


# --- Node.js -----------------------------------------------------------------

def _detect_node(det: Detection, cwd: str, d: Path) -> bool:
    manifest = d / "package.json"
    if not manifest.is_file():
        return False
    try:
        pkg = json.loads(_read(manifest) or "{}")
    except json.JSONDecodeError:
        det.notes.append(f"{_rel(cwd, 'package.json')} is not valid JSON; skipped.")
        return False
    scripts = pkg.get("scripts") or {}

    if (d / "pnpm-lock.yaml").exists():
        pm = "pnpm"
        run = lambda s: f"pnpm run {s}"  # noqa: E731
        det.bootstrap.append(_plain_install(cwd, "pnpm-install", "pnpm install --frozen-lockfile", ["package.json", "pnpm-lock.yaml"]))
    elif (d / "yarn.lock").exists():
        pm = "yarn"
        run = lambda s: f"yarn run {s}"  # noqa: E731
        det.bootstrap.append(_plain_install(cwd, "yarn-install", "yarn install --frozen-lockfile", ["package.json", "yarn.lock"]))
    else:
        pm = "npm"
        run = lambda s: "npm test" if s == "test" else f"npm run {s}"  # noqa: E731
        if (d / "package-lock.json").exists():
            det.bootstrap.append(_npm_shared_cache(cwd))
        else:
            det.bootstrap.append(_plain_install(cwd, "npm-install", "npm install --no-audit --no-fund", ["package.json"]))
            det.notes.append(f"{_rel(cwd, 'package.json')} has no package-lock.json; the shared npm cache is only used with a lockfile.")

    def pick(*names):
        return next((n for n in names if n in scripts), None)

    test = "test" if "test" in scripts and NPM_PLACEHOLDER_TEST not in str(scripts["test"]) else None
    typecheck = pick("typecheck", "type-check", "check-types", "tsc")
    lint = pick("lint")
    build = pick("build")
    e2e = pick("test:e2e", "e2e", "test-e2e")

    p, when = _prefix(cwd), _when(cwd)
    found = []
    if test:
        body = str(scripts["test"])
        runner = "vitest" if "vitest" in body else ("jest" if "jest" in body else "test")
        check = {"name": f"{p}{runner}", "cwd": cwd, "command": run("test"), "when": when, "timeout": 1800}
        det.add(("fast", "batch_full"), check)
        found.append("test")
    if typecheck:
        det.add(("fast", "batch_fast"), {"name": f"{p}typecheck", "cwd": cwd, "command": run(typecheck), "when": when, "timeout": 1200})
        found.append(typecheck)
    if lint:
        det.add(("fast", "batch_fast"), {"name": f"{p}lint", "cwd": cwd, "command": run(lint), "when": when, "timeout": 1200})
        found.append("lint")
    if build:
        det.add(("full", "batch_full"), {"name": f"{p}build", "cwd": cwd, "command": run(build), "when": when, "timeout": 1800})
        found.append("build")
    if e2e:
        # Only Playwright understands the --workers flag Autopilot adds to
        # checks named "playwright"/"*-e2e"; other runners get a neutral name.
        name = f"{p}playwright" if "playwright" in str(scripts[e2e]) else f"{p}e2e-tests"
        det.add(("full", "batch_full"), {"name": name, "cwd": cwd, "command": run(e2e), "when": when, "timeout": 7200})
        found.append(e2e)

    det.artifacts(
        [_rel(cwd, x) for x in (".next", "coverage", "playwright-report", "test-results")],
        done_only=[_rel(cwd, "node_modules")],
    )
    det.summary.append(f"Node.js ({pm}) in {cwd or '.'}/: " + (", ".join(found) if found else "no test/lint/build scripts"))
    for vf in (d / ".nvmrc", d / ".node-version"):
        m = re.match(r"\s*v?(\d+)", _read(vf))
        if m and "expected_node_major" not in det.toolchain:
            det.toolchain["expected_node_major"] = int(m.group(1))
    return True


def _npm_shared_cache(cwd: str) -> dict:
    return {
        "name": f"{_prefix(cwd)}npm-ci",
        "cwd": cwd,
        "strategy": "npm_shared_cache",
        "command": "npm ci --no-audit --no-fund",
        "manifest": "package.json",
        "lockfile": "package-lock.json",
        "key_files": ["package-lock.json", "package.json"],
        "cache_probe": "node_modules/.package-lock.json",
        "link_path": "node_modules",
        "if_missing": "node_modules/.package-lock.json",
        "rerun_if_changed": [_rel(cwd, "package.json"), _rel(cwd, "package-lock.json")],
        "timeout": 2400,
    }


def _plain_install(cwd: str, name: str, command: str, watched: list[str]) -> dict:
    return {
        "name": f"{_prefix(cwd)}{name}",
        "cwd": cwd,
        "command": command,
        "if_missing": "node_modules",
        "rerun_if_changed": [_rel(cwd, x) for x in watched],
        "timeout": 2400,
    }


# --- Python ------------------------------------------------------------------

PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt", "Pipfile")


def _detect_python(det: Detection, cwd: str, d: Path) -> bool:
    if not any((d / m).exists() for m in PY_MARKERS):
        return False
    text = " ".join(_read(d / m) for m in PY_MARKERS).lower()
    p, when = _prefix(cwd), _when(cwd)
    found = []
    if "pytest" in text or (d / "tests").is_dir() or (d / "pytest.ini").exists() or (d / "conftest.py").exists():
        det.add(("fast", "batch_full"), {"name": f"{p}pytest", "cwd": cwd, "command": "python -m pytest -q", "when": when, "timeout": 3600})
        found.append("pytest")
    if "ruff" in text or (d / "ruff.toml").exists() or (d / ".ruff.toml").exists():
        det.add(("fast", "batch_fast"), {"name": f"{p}ruff", "cwd": cwd, "command": "python -m ruff check .", "when": when, "timeout": 600})
        found.append("ruff")
    if "[tool.mypy]" in text or (d / "mypy.ini").exists():
        det.add(("fast", "batch_fast"), {"name": f"{p}mypy", "cwd": cwd, "command": "python -m mypy .", "when": when, "timeout": 1200})
        found.append("mypy")
    det.artifacts([_rel(cwd, x) for x in (".coverage", "coverage.xml", "htmlcov")])
    det.summary.append(f"Python in {cwd or '.'}/: " + (", ".join(found) if found else "no pytest/ruff/mypy found"))
    det.notes.append("Python checks run with the `python` on PATH: run Autopilot from the target project's virtualenv, or put its interpreter path in the commands.")
    m = re.match(r"\s*(\d+\.\d+)", _read(d / ".python-version"))
    if m and "expected_python_major_minor" not in det.toolchain:
        det.toolchain["expected_python_major_minor"] = m.group(1)
    return True


# --- Go / Rust ---------------------------------------------------------------

def _detect_go(det: Detection, cwd: str, d: Path) -> bool:
    if not (d / "go.mod").is_file():
        return False
    p, when = _prefix(cwd), _when(cwd)
    det.add(("fast", "batch_fast"), {"name": f"{p}go-vet", "cwd": cwd, "command": "go vet ./...", "when": when, "timeout": 1200})
    det.add(("fast", "batch_full"), {"name": f"{p}go-test", "cwd": cwd, "command": "go test ./...", "when": when, "timeout": 3600})
    det.add(("full", "batch_full"), {"name": f"{p}go-build", "cwd": cwd, "command": "go build ./...", "when": when, "timeout": 1800})
    det.summary.append(f"Go in {cwd or '.'}/: vet, test, build")
    return True


def _detect_rust(det: Detection, cwd: str, d: Path) -> bool:
    if not (d / "Cargo.toml").is_file():
        return False
    p, when = _prefix(cwd), _when(cwd)
    det.add(("fast", "batch_full"), {"name": f"{p}cargo-test", "cwd": cwd, "command": "cargo test", "when": when, "timeout": 3600})
    det.add(("batch_fast",), {"name": f"{p}cargo-check", "cwd": cwd, "command": "cargo check", "when": when, "timeout": 1800})
    det.add(("full", "batch_full"), {"name": f"{p}cargo-build", "cwd": cwd, "command": "cargo build", "when": when, "timeout": 1800})
    det.summary.append(f"Rust in {cwd or '.'}/: test, check, build")
    return True


def detect_project(repo: str | Path) -> Detection:
    repo = Path(repo)
    det = Detection()
    root_pkg = repo / "package.json"
    node_workspace = (repo / "pnpm-workspace.yaml").exists() or (
        root_pkg.is_file() and '"workspaces"' in _read(root_pkg)
    )
    root_has = {"go": (repo / "go.mod").is_file(), "rust": (repo / "Cargo.toml").is_file()}
    for cwd, d in _candidate_dirs(repo):
        nested = cwd != ""
        # Workspace/module roots already cover their members.
        if not (nested and node_workspace):
            _detect_node(det, cwd, d)
        _detect_python(det, cwd, d)
        if not (nested and root_has["go"]):
            _detect_go(det, cwd, d)
        if not (nested and root_has["rust"]):
            _detect_rust(det, cwd, d)
    return det


def apply_detection(cfg: dict, det: Detection) -> dict:
    cfg["workspace_bootstrap"] = det.bootstrap
    validation = cfg.setdefault("validation", {})
    for phase in PHASES:
        validation[phase] = det.validation[phase]
    cfg["protected_paths"] = [".env"]
    dep = cfg.setdefault("dependency_cache", {})
    dep["enabled"] = any(b.get("strategy") == "npm_shared_cache" for b in det.bootstrap)
    dep["done_artifacts"] = det.done_artifacts
    cfg.setdefault("integration", {})["generated_artifacts"] = det.generated_artifacts
    cfg.setdefault("toolchain", {}).update(det.toolchain)
    return cfg


# --- Repository facts for interactive init -----------------------------------

def _git(repo: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def parse_github_slug(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def infer_repo_slug(repo: str | Path) -> str | None:
    return parse_github_slug(_git(Path(repo), "remote", "get-url", "origin"))


def infer_base_branch(repo: str | Path) -> str | None:
    ref = _git(Path(repo), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if ref and ref.startswith("refs/remotes/origin/"):
        return ref[len("refs/remotes/origin/"):]
    return _git(Path(repo), "branch", "--show-current") or None
