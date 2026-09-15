from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .validator import _condition_matches
from .shell import run_shell_stream


@dataclass
class BootstrapResult:
    name: str
    returncode: int
    log: str
    skipped: bool = False
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.skipped or self.returncode == 0


def _safe_remove_generated_dir(path: Path) -> None:
    """Remove a generated directory or directory link without following links.

    On Windows a junction is best removed with plain `rmdir <path>` (without /S),
    which removes the junction itself but never traverses into the target. If the
    path is a normal non-empty directory that command fails and we fall back to
    shutil.rmtree for the generated local directory.
    """
    if not os.path.lexists(path):
        return

    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
        return

    if os.name == "nt":
        p = subprocess.run(
            ["cmd", "/c", "rmdir", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if p.returncode == 0 and not os.path.lexists(path):
            return

    shutil.rmtree(path)


def _create_directory_link(link: Path, target: Path) -> None:
    """Create a directory link. Use NTFS junctions on Windows for no-admin use."""
    link.parent.mkdir(parents=True, exist_ok=True)
    _safe_remove_generated_dir(link)

    if os.name == "nt":
        p = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if p.returncode != 0:
            raw = (p.stdout or "") + "\n" + (p.stderr or "")
            raise RuntimeError(f"No se pudo crear junction {link} -> {target}: {raw.strip()}")
    else:
        os.symlink(target, link, target_is_directory=True)


def _hash_dependency_inputs(cwd: Path, spec: dict) -> tuple[str, list[str]]:
    manifest = spec.get("manifest", "package.json")
    lockfile = spec.get("lockfile", "package-lock.json")
    key_files = list(spec.get("key_files", [lockfile, manifest]))
    if not key_files:
        key_files = [lockfile, manifest]

    h = hashlib.sha256()
    normalized: list[str] = []
    for rel in key_files:
        rel = str(rel).replace("\\", "/")
        p = cwd / rel
        if not p.exists():
            raise FileNotFoundError(f"Falta archivo de dependencias requerido: {p}")
        normalized.append(rel)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest(), normalized


def _cache_ready(cache_dir: Path, probe: str) -> bool:
    marker = cache_dir / ".abi-autopilot-ready.json"
    return marker.exists() and (cache_dir / probe).exists()


def _remove_stale_build_dirs(parent: Path, key: str) -> None:
    for p in parent.glob(f".{key}.building-*"):
        try:
            _safe_remove_generated_dir(p)
        except Exception:
            # Generated cache residue is non-critical; a future disk cleanup can remove it.
            pass


def _acquire_cache_lock(lock_dir: Path, cache_dir: Path, probe: str, timeout: int, stale_seconds: int) -> bool:
    """Acquire a directory lock. Returns False when another builder finished first."""
    start = time.time()
    while True:
        if _cache_ready(cache_dir, probe):
            return False
        try:
            lock_dir.mkdir(parents=False)
            (lock_dir / "owner.json").write_text(
                json.dumps({
                    "pid": os.getpid(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }, indent=2),
                encoding="utf-8",
            )
            return True
        except FileExistsError:
            try:
                age = time.time() - lock_dir.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_seconds:
                try:
                    shutil.rmtree(lock_dir)
                    continue
                except Exception:
                    pass
            if time.time() - start >= timeout:
                raise TimeoutError(f"Timeout esperando cache lock: {lock_dir}")
            time.sleep(2)


def _dependency_cache_root(cfg: dict) -> Path:
    dep = cfg.get("dependency_cache", {})
    root = dep.get("root")
    if root:
        return Path(root)
    repo = Path(cfg["repo_path"])
    return repo.parent / f"{repo.name}-autopilot-deps"


def _prepare_npm_shared_cache(
    cfg: dict,
    spec: dict,
    worktree: Path,
    cwd: Path,
    run_dir: Path,
) -> BootstrapResult:
    name = spec["name"]
    dep_cfg = cfg.get("dependency_cache", {})
    log_path = run_dir / f"bootstrap-{name}.log"
    build_log = run_dir / f"bootstrap-{name}-cache-build.log"

    try:
        key, key_files = _hash_dependency_inputs(cwd, spec)
    except Exception as e:
        log_path.write_text(str(e) + "\n", encoding="utf-8")
        return BootstrapResult(name, 2, str(log_path), False, str(e))

    probe = spec.get("cache_probe") or spec.get("if_missing") or "node_modules/.package-lock.json"
    cache_root = _dependency_cache_root(cfg)
    cache_parent = cache_root / "npm"
    cache_parent.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_parent / key
    target_modules = cache_dir / "node_modules"
    link_rel = spec.get("link_path", "node_modules")
    link_path = worktree / link_rel

    # Remove the old per-worktree install if present. It would shadow the shared
    # parent dependency link and is the main source of disk duplication in v1.4.
    local_modules = cwd / "node_modules"
    if local_modules != link_path and os.path.lexists(local_modules):
        print(f"BOOTSTRAP cache: eliminando node_modules local duplicado: {local_modules}")
        _safe_remove_generated_dir(local_modules)

    cache_hit = _cache_ready(cache_dir, probe)
    built = False

    if not cache_hit:
        min_free_gb = float(dep_cfg.get("min_free_gb", 8))
        free_gb = shutil.disk_usage(cache_parent).free / (1024 ** 3)
        if free_gb < min_free_gb:
            msg = (
                f"Espacio insuficiente para construir cache de dependencias: "
                f"{free_gb:.1f} GB libres, mínimo configurado {min_free_gb:.1f} GB."
            )
            log_path.write_text(msg + "\n", encoding="utf-8")
            return BootstrapResult(name, 28, str(log_path), False, msg)

        lock_dir = cache_parent / f".{key}.lock"
        lock_timeout = int(dep_cfg.get("lock_timeout_seconds", 1800))
        stale_seconds = int(dep_cfg.get("stale_lock_seconds", 7200))
        acquired = False
        try:
            acquired = _acquire_cache_lock(lock_dir, cache_dir, probe, lock_timeout, stale_seconds)
            if acquired and not _cache_ready(cache_dir, probe):
                _remove_stale_build_dirs(cache_parent, key)
                staging = cache_parent / f".{key}.building-{os.getpid()}-{uuid.uuid4().hex[:8]}"
                staging.mkdir(parents=True, exist_ok=False)

                # npm ci only needs the package manifests for this repo. Copy .npmrc
                # too when present so registry settings remain consistent.
                copy_files = set(key_files)
                for rel in (spec.get("manifest", "package.json"), spec.get("lockfile", "package-lock.json"), ".npmrc"):
                    if (cwd / rel).exists():
                        copy_files.add(rel)
                for rel in sorted(copy_files):
                    src = cwd / rel
                    dst = staging / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

                print(f"\n=== BOOTSTRAP CACHE BUILD · {name} · {key[:12]} ===")
                rc = run_shell_stream(spec["command"], staging, build_log, int(spec.get("timeout", 1800)))
                if rc != 0:
                    _safe_remove_generated_dir(staging)
                    return BootstrapResult(name, rc, str(build_log), False, f"cache build {key[:12]} falló")

                if not (staging / probe).exists():
                    msg = f"Cache npm terminó rc=0 pero falta probe requerido: {probe}"
                    with build_log.open("a", encoding="utf-8", errors="replace") as f:
                        f.write("\n" + msg + "\n")
                    _safe_remove_generated_dir(staging)
                    return BootstrapResult(name, 3, str(build_log), False, msg)

                (staging / ".abi-autopilot-ready.json").write_text(
                    json.dumps({
                        "version": 1,
                        "key": key,
                        "key_files": key_files,
                        "command": spec["command"],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                if cache_dir.exists():
                    _safe_remove_generated_dir(cache_dir)
                staging.replace(cache_dir)
                built = True
        except Exception as e:
            log_path.write_text(f"Dependency cache error: {e}\n", encoding="utf-8")
            return BootstrapResult(name, 4, str(log_path), False, str(e))
        finally:
            if acquired and lock_dir.exists():
                try:
                    shutil.rmtree(lock_dir)
                except Exception:
                    pass

    if not _cache_ready(cache_dir, probe):
        msg = f"Cache no quedó listo: {cache_dir}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        return BootstrapResult(name, 5, str(log_path), False, msg)

    try:
        _create_directory_link(link_path, target_modules)
    except Exception as e:
        log_path.write_text(f"No se pudo enlazar cache: {e}\n", encoding="utf-8")
        return BootstrapResult(name, 6, str(log_path), False, str(e))

    # Verify package resolution from the actual frontend cwd. The probe is tested
    # against the shared target, while `npm run`/Node resolve packages through the
    # parent worktree/node_modules junction.
    if not (cache_dir / probe).exists():
        msg = f"Probe de cache ausente tras link: {probe}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        return BootstrapResult(name, 7, str(log_path), False, msg)

    status = "built" if built else "hit"
    summary = (
        f"strategy=npm_shared_cache\n"
        f"status={status}\n"
        f"key={key}\n"
        f"cache={cache_dir}\n"
        f"link={link_path} -> {target_modules}\n"
        f"frontend_local_node_modules_removed={local_modules != link_path}\n"
    )
    log_path.write_text(summary, encoding="utf-8")
    print(f"BOOTSTRAP cache {status}: {key[:12]} · {link_path} -> {target_modules}")
    return BootstrapResult(name, 0, str(log_path), False, f"cache {status} {key[:12]}")


def prepare_workspace(cfg: dict, worktree: Path, changed: list[str], run_dir: Path, force: bool = False) -> list[BootstrapResult]:
    """Prepare ignored dependencies inside a worktree.

    v1.5 can share one immutable npm dependency install across worktrees keyed by
    package manifests. Each issue gets a lightweight parent `node_modules` junction
    so package resolution works without materializing a full copy in every frontend.
    Legacy bootstrap commands remain supported for custom configs.
    """
    results: list[BootstrapResult] = []
    dep_enabled = bool(cfg.get("dependency_cache", {}).get("enabled", True))

    for spec in cfg.get("workspace_bootstrap", []):
        if not _condition_matches(spec.get("when", "always"), changed):
            continue
        cwd = worktree / spec.get("cwd", "")
        if not cwd.exists():
            results.append(BootstrapResult(spec["name"], 0, "", True, "cwd ausente"))
            continue

        if spec.get("strategy") == "npm_shared_cache" and dep_enabled:
            result = _prepare_npm_shared_cache(cfg, spec, worktree, cwd, run_dir)
            results.append(result)
            if not result.passed:
                break
            continue

        # Legacy v1.4/custom bootstrap path.
        missing = spec.get("if_missing")
        rerun_if = [x.replace("\\", "/") for x in spec.get("rerun_if_changed", [])]
        need = bool(force)
        why: list[str] = ["force repair"] if force else []

        if missing and not (cwd / missing).exists():
            need = True
            why.append(f"falta {missing}")
        if any(x in changed for x in rerun_if):
            need = True
            why.append("lock/config cambió")
        if spec.get("always", False):
            need = True
            why.append("always")

        if not need:
            results.append(BootstrapResult(spec["name"], 0, "", True, "ya preparado"))
            continue

        log_path = run_dir / f"bootstrap-{spec['name']}.log"
        print(f"\n=== BOOTSTRAP · {spec['name']} ({', '.join(why)}) ===")
        rc = run_shell_stream(spec["command"], cwd, log_path, int(spec.get("timeout", 1800)))
        results.append(BootstrapResult(spec["name"], rc, str(log_path), False, ", ".join(why)))
        if rc != 0:
            break
    return results


def cleanup_done_artifacts(cfg: dict, worktree: Path) -> list[str]:
    """Delete only disposable build/test artifacts after a successful issue.

    We intentionally keep source, commits, the worktree, reports copied into runtime,
    and the shared dependency cache. A cleanup failure never invalidates green code.
    """
    dep_cfg = cfg.get("dependency_cache", {})
    if not dep_cfg.get("cleanup_done_artifacts", True):
        return []

    configured = dep_cfg.get(
        "done_artifacts",
        [
            "frontend/.next", "frontend/node_modules",
            "frontend/playwright-report", "frontend/test-results",
            ".coverage", "backend/.coverage", "coverage.xml", "backend/coverage.xml",
        ],
    )
    messages: list[str] = []
    for rel in configured:
        rel = str(rel).replace("\\", "/").strip("/")
        if not rel or rel in (".", "..") or rel.startswith("../"):
            messages.append(f"SKIP unsafe cleanup path: {rel}")
            continue
        p = worktree / rel
        if not os.path.lexists(p):
            continue
        # Never delete a path Git tracks, even if an operator accidentally added
        # it to the cleanup list. This keeps cleanup strictly regenerable-only.
        tracked = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--error-unmatch", "--", rel],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0
        if tracked:
            messages.append(f"SKIP tracked path: {rel}")
            continue
        try:
            _safe_remove_generated_dir(p)
            messages.append(f"removed {rel}")
        except Exception as e:
            messages.append(f"WARN {rel}: {e}")
    return messages
