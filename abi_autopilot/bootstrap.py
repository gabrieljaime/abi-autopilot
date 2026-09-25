# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .validator import _condition_matches
from .shell import run_shell_stream


# Resultados internos de bootstrap. Los returncode existentes se conservan
# (2 key, 3 probe, 4 error genérico, 5 no listo, 6 link, 7 probe post-link, 28
# disco); el status distingue la fase que falló sin depender del rc.
STATUS_OK = "OK"
STATUS_INSTALL_FAILED = "INSTALL_FAILED"
STATUS_CACHE_VALIDATE_FAILED = "CACHE_VALIDATE_FAILED"
STATUS_CACHE_PUBLISH_FAILED = "CACHE_PUBLISH_FAILED"
STATUS_CACHE_ATTACH_FAILED = "CACHE_ATTACH_FAILED"
STATUS_BOOTSTRAP_ERROR = "BOOTSTRAP_ERROR"

RC_CACHE_PUBLISH_FAILED = 8

READY_MARKER = ".abi-autopilot-ready.json"
BUILDING_OWNER = ".abi-autopilot-building.json"
MARKER_SCHEMA_VERSION = 1

# Backoff acotado ante errores transitorios de rename en Windows (antivirus,
# indexador, handles residuales de npm). Un intento inicial + un retry por delay.
PUBLISH_RETRY_DELAYS: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0)
_TRANSIENT_WINERRORS = frozenset({5, 32})


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclass
class BootstrapResult:
    name: str
    returncode: int
    log: str
    skipped: bool = False
    reason: str = ""
    status: str = ""

    @property
    def passed(self) -> bool:
        return self.skipped or self.returncode == 0

    def describe(self) -> str:
        tag = f" {self.status}" if self.status and self.status != STATUS_OK else ""
        return f"{self.name}: rc={self.returncode}{tag} log={self.log}"


class CachePublishError(Exception):
    """El cache se construyó bien pero no pudo publicarse tras agotar los retries."""

    def __init__(self, message: str, staging: Path, attempts: int, last_error: BaseException | None):
        super().__init__(message)
        self.staging = staging
        self.attempts = attempts
        self.last_error = last_error


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
            raise RuntimeError(f"Could not create junction {link} -> {target}: {raw.strip()}")
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
            raise FileNotFoundError(f"Required dependency file is missing: {p}")
        normalized.append(rel)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest(), normalized


def _log(msg: str) -> None:
    print(f"BOOTSTRAP cache: {msg}", flush=True)


def _cache_valid(cache_dir: Path, key: str, probe: str) -> bool:
    """Contrato de validez: marker legible + misma key + probe presente.

    Que la carpeta exista no basta: un cache a medio construir, de otra key o
    marcado por una versión incompatible no se usa. Los markers previos (sin
    `schema_version`, con `key`) siguen siendo válidos.
    """
    try:
        data = json.loads((cache_dir / READY_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if (data.get("cache_key") or data.get("key")) != key:
        return False
    if data.get("schema_version", MARKER_SCHEMA_VERSION) != MARKER_SCHEMA_VERSION:
        return False
    return (cache_dir / probe).exists()


def _write_ready_marker(dest: Path, key: str, kind: str, key_files: list[str], command: str) -> None:
    """Escribe el marker de forma atómica: nunca queda un marker parcial."""
    payload = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "kind": kind,
        "cache_key": key,
        "key": key,  # alias legado, leído por versiones anteriores
        "key_files": key_files,
        "command": command,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = dest / (READY_MARKER + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dest / READY_MARKER)


def _is_transient_publish_error(e: BaseException) -> bool:
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in _TRANSIENT_WINERRORS


def _describe_os_error(e: BaseException | None) -> str:
    if e is None:
        return "desconocido"
    winerror = getattr(e, "winerror", None)
    if winerror:
        return f"WinError{winerror}"
    return type(e).__name__


def _resolve_existing_destination(cache_dir: Path, key: str, probe: str) -> str:
    """Clasifica el destino final: 'absent' | 'valid' | 'quarantined' | 'busy'.

    Un destino inválido no se borra ni se usa: se renombra (atómico, sólo un
    proceso gana) a `.<key>.corrupt-*`. Nadie lo enlaza porque no valida, y el
    cleanup por antigüedad lo elimina más tarde.
    """
    if not os.path.lexists(cache_dir):
        return "absent"
    if _cache_valid(cache_dir, key, probe):
        return "valid"
    corrupt = cache_dir.with_name(f".{key}.corrupt-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        os.replace(cache_dir, corrupt)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "busy"
    _log(f"target {cache_dir.name[:12]} is invalid; quarantined as {corrupt.name}")
    return "quarantined"


def _publish_cache(staging: Path, cache_dir: Path, key: str, probe: str) -> str:
    """Publica `staging` como `cache_dir` con retry acotado.

    Devuelve 'published' o 'concurrent' (otro proceso publicó un cache válido
    para la misma key; el staging propio se descarta). Si se agota el budget
    lanza CachePublishError y deja el staging intacto para reutilizarlo.
    """
    delays = tuple(PUBLISH_RETRY_DELAYS)
    total = len(delays)
    last: BaseException | None = None

    for attempt in range(total + 1):
        state = _resolve_existing_destination(cache_dir, key, probe)
        if state == "valid":
            _discard_staging(staging)
            return "concurrent"
        if state == "busy":
            last = last or OSError(f"invalid target cannot be removed: {cache_dir}")
        else:
            try:
                os.replace(staging, cache_dir)
                return "published"
            except OSError as e:
                last = e
                if _cache_valid(cache_dir, key, probe):
                    _discard_staging(staging)
                    return "concurrent"
                if not _is_transient_publish_error(e) and not os.path.lexists(cache_dir):
                    raise CachePublishError(
                        f"rename cannot be retried: {e}", staging, attempt + 1, e
                    ) from e
        if attempt < total:
            _log(f"publish=RETRY {attempt + 1}/{total} {_describe_os_error(last)}")
            _sleep(delays[attempt])

    raise CachePublishError(
        f"publish failed after {total} retries: {last}", staging, total + 1, last
    ) from last


def _discard_staging(staging: Path) -> None:
    try:
        _safe_remove_generated_dir(staging)
    except Exception:
        pass  # residuo generado; el cleanup por antigüedad lo recoge


def _dir_age_seconds(p: Path, now: float) -> float:
    newest = 0.0
    for candidate in (p, p / BUILDING_OWNER):
        try:
            newest = max(newest, candidate.stat().st_mtime)
        except OSError:
            pass
    return now - newest if newest else 0.0


def _cleanup_stale_dirs(parent: Path, key: str, stale_seconds: float, keep: Path | None = None) -> None:
    """Elimina `.building-*` y `.corrupt-*` de esta key sólo si superan la antigüedad.

    Un building fresco puede pertenecer a otro proceso vivo; no se toca. El
    umbral debe superar el timeout de build (ver _prepare_npm_shared_cache).
    """
    now = time.time()
    for pattern in (f".{key}.building-*", f".{key}.corrupt-*"):
        for p in parent.glob(pattern):
            if keep is not None and p == keep:
                continue
            if _dir_age_seconds(p, now) < stale_seconds:
                continue
            _log(f"removing abandoned leftover: {p.name}")
            _discard_staging(p)


def _adopt_ready_staging(parent: Path, cache_dir: Path, key: str, probe: str) -> str | None:
    """Reutiliza un `.building-*` completo (marker escrito) de un publish fallido.

    El marker se escribe al final del build, así que un staging con marker válido
    es un install terminado. Devuelve el resultado de publicar o None si no hay.
    """
    for p in sorted(parent.glob(f".{key}.building-*")):
        if _cache_valid(p, key, probe):
            _log(f"reusing previous complete build {p.name}")
            return _publish_cache(p, cache_dir, key, probe)
    return None


def _owns_lock(lock_dir: Path, token: str) -> bool:
    try:
        return json.loads((lock_dir / "owner.json").read_text(encoding="utf-8")).get("token") == token
    except (OSError, ValueError):
        return False


def _acquire_cache_lock(lock_dir: Path, cache_dir: Path, key: str, probe: str, timeout: int, stale_seconds: int) -> str | None:
    """Acquire a directory lock. Returns the ownership token, or None when another builder finished first."""
    start = time.time()
    while True:
        if _cache_valid(cache_dir, key, probe):
            return None
        try:
            lock_dir.mkdir(parents=False)
            token = uuid.uuid4().hex
            (lock_dir / "owner.json").write_text(
                json.dumps({
                    "pid": os.getpid(),
                    "token": token,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }, indent=2),
                encoding="utf-8",
            )
            return token
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
                raise TimeoutError(f"Timed out waiting for cache lock: {lock_dir}")
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
        return BootstrapResult(name, 2, str(log_path), False, str(e), STATUS_BOOTSTRAP_ERROR)

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
        print(f"BOOTSTRAP cache: removing duplicate local node_modules: {local_modules}")
        _safe_remove_generated_dir(local_modules)

    cache_hit = _cache_valid(cache_dir, key, probe)
    built = False
    concurrent = False

    if not cache_hit:
        min_free_gb = float(dep_cfg.get("min_free_gb", 8))
        free_gb = shutil.disk_usage(cache_parent).free / (1024 ** 3)
        if free_gb < min_free_gb:
            msg = (
                f"Not enough disk space to build the dependency cache: "
                f"{free_gb:.1f} GB free, configured minimum {min_free_gb:.1f} GB."
            )
            log_path.write_text(msg + "\n", encoding="utf-8")
            return BootstrapResult(name, 28, str(log_path), False, msg, STATUS_BOOTSTRAP_ERROR)

        lock_dir = cache_parent / f".{key}.lock"
        lock_timeout = int(dep_cfg.get("lock_timeout_seconds", 1800))
        stale_seconds = int(dep_cfg.get("stale_lock_seconds", 7200))
        build_timeout = int(spec.get("timeout", 1800))
        # Un building fresco puede ser de otro proceso vivo: sólo es residuo si
        # supera holgadamente el timeout de build.
        stale_build_seconds = max(stale_seconds, 2 * build_timeout)
        lock_token: str | None = None
        staging: Path | None = None
        try:
            lock_token = _acquire_cache_lock(lock_dir, cache_dir, key, probe, lock_timeout, stale_seconds)
            if lock_token is not None and not _cache_valid(cache_dir, key, probe):
                _cleanup_stale_dirs(cache_parent, key, stale_build_seconds)

                outcome = _adopt_ready_staging(cache_parent, cache_dir, key, probe)
                if outcome is None:
                    staging = cache_parent / f".{key}.building-{os.getpid()}-{uuid.uuid4().hex[:8]}"
                    staging.mkdir(parents=True, exist_ok=False)
                    (staging / BUILDING_OWNER).write_text(
                        json.dumps({
                            "pid": os.getpid(),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }),
                        encoding="utf-8",
                    )

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
                    _log(f"key={key}")
                    _log(f"building={staging}")
                    rc = run_shell_stream(spec["command"], staging, build_log, build_timeout)
                    if rc != 0:
                        _log(f"install=FAIL rc={rc} log={build_log}")
                        _safe_remove_generated_dir(staging)
                        return BootstrapResult(
                            name, rc, str(build_log), False,
                            f"INSTALL_FAILED: cache build {key[:12]} failed (rc={rc})", STATUS_INSTALL_FAILED,
                        )
                    _log("install=PASS")

                    if not (staging / probe).exists():
                        msg = f"CACHE_VALIDATE_FAILED: npm finished rc=0 but the required probe is missing: {probe}"
                        with build_log.open("a", encoding="utf-8", errors="replace") as f:
                            f.write("\n" + msg + "\n")
                        _log(f"validation=FAIL missing probe {probe}")
                        _safe_remove_generated_dir(staging)
                        return BootstrapResult(name, 3, str(build_log), False, msg, STATUS_CACHE_VALIDATE_FAILED)
                    _log("validation=PASS")

                    # El marker se escribe al final del build y antes de publicar:
                    # un staging con marker es un install completo y reutilizable.
                    _write_ready_marker(staging, key, name, key_files, spec["command"])
                    outcome = _publish_cache(staging, cache_dir, key, probe)
                    staging = None
                else:
                    staging = None

                if outcome == "concurrent":
                    concurrent = True
                    _log("CACHE published concurrently by another process - using existing cache")
                else:
                    built = True
                    _log("publish=PASS")
                    _log("cache=READY")
        except CachePublishError as e:
            msg = (
                f"CACHE_BUILD_OK / CACHE_PUBLISH_FAILED: npm ci and validation passed, but "
                f"{cache_dir} could not be published after {e.attempts} attempts ({_describe_os_error(e.last_error)}). "
                f"Build preserved at {e.staging}; the next bootstrap reuses it without reinstalling."
            )
            _log(f"publish=FAILED after {max(e.attempts - 1, 0)} retries")
            _log(f"building preserved at {e.staging}")
            log_path.write_text(
                msg + "\n\n" + "".join(traceback.format_exception(e)), encoding="utf-8"
            )
            return BootstrapResult(name, RC_CACHE_PUBLISH_FAILED, str(log_path), False, msg, STATUS_CACHE_PUBLISH_FAILED)
        except Exception as e:
            log_path.write_text(
                f"Dependency cache error: {e}\n\n{traceback.format_exc()}", encoding="utf-8"
            )
            return BootstrapResult(name, 4, str(log_path), False, str(e), STATUS_BOOTSTRAP_ERROR)
        finally:
            # Sólo se libera el lock propio; si otro proceso lo recuperó por stale no se toca.
            if lock_token is not None and lock_dir.exists() and _owns_lock(lock_dir, lock_token):
                try:
                    shutil.rmtree(lock_dir)
                except Exception:
                    pass

    if not _cache_valid(cache_dir, key, probe):
        msg = f"CACHE_VALIDATE_FAILED: cache is not ready: {cache_dir}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        return BootstrapResult(name, 5, str(log_path), False, msg, STATUS_CACHE_VALIDATE_FAILED)

    try:
        _create_directory_link(link_path, target_modules)
    except Exception as e:
        log_path.write_text(f"CACHE_ATTACH_FAILED: could not link the cache: {e}\n", encoding="utf-8")
        return BootstrapResult(name, 6, str(log_path), False, f"CACHE_ATTACH_FAILED: {e}", STATUS_CACHE_ATTACH_FAILED)

    # Verify package resolution from the actual frontend cwd. The probe is tested
    # against the shared target, while `npm run`/Node resolve packages through the
    # parent worktree/node_modules junction.
    if not (cache_dir / probe).exists():
        msg = f"CACHE_ATTACH_FAILED: cache probe missing after linking: {probe}"
        log_path.write_text(msg + "\n", encoding="utf-8")
        return BootstrapResult(name, 7, str(log_path), False, msg, STATUS_CACHE_ATTACH_FAILED)

    status = "built" if built else "hit"
    summary = (
        f"strategy=npm_shared_cache\n"
        f"status={status}\n"
        f"published_concurrently={str(concurrent).lower()}\n"
        f"key={key}\n"
        f"cache={cache_dir}\n"
        f"link={link_path} -> {target_modules}\n"
        f"frontend_local_node_modules_removed={local_modules != link_path}\n"
    )
    log_path.write_text(summary, encoding="utf-8")
    print(f"BOOTSTRAP cache {status}: {key[:12]} · {link_path} -> {target_modules}")
    return BootstrapResult(name, 0, str(log_path), False, f"cache {status} {key[:12]}", STATUS_OK)


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
            results.append(BootstrapResult(spec["name"], 0, "", True, "cwd missing"))
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
            why.append(f"missing {missing}")
        if any(x in changed for x in rerun_if):
            need = True
            why.append("lock/config changed")
        if spec.get("always", False):
            need = True
            why.append("always")

        if not need:
            results.append(BootstrapResult(spec["name"], 0, "", True, "already prepared"))
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
