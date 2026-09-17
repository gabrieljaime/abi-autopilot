from __future__ import annotations
import re
import json
from pathlib import Path
from .shell import run_capture, CommandError


def git(repo: str | Path, args: list[str], check: bool=True):
    return run_capture(["git", *args], cwd=repo, check=check)


def slugify(title: str, max_len: int=42) -> str:
    s=title.lower()
    s=re.sub(r"\[[^\]]+\]","",s)
    s=re.sub(r"[^a-z0-9]+","-",s).strip("-")
    return (s[:max_len].rstrip("-") or "issue")


def fetch(repo_path: str):
    git(repo_path,["fetch","origin","--prune"])


def remote_branch_sha(repo_path: str, branch: str) -> str:
    p=git(repo_path,["ls-remote","origin",f"refs/heads/{branch}"])
    line=(p.stdout or "").strip()
    if not line:
        raise CommandError(f"No existe origin/{branch}")
    return line.split()[0]


def create_or_reuse_worktree(repo_path: str, root: str, issue_number: int, title: str, base_branch: str):
    fetch(repo_path)
    base_sha=remote_branch_sha(repo_path,base_branch)
    rootp=Path(root)
    wt=rootp/f"issue-{issue_number}"
    branch=f"agent/issue-{issue_number}-{slugify(title)}"
    rootp.mkdir(parents=True,exist_ok=True)
    meta_dir=rootp/".metadata"
    meta_dir.mkdir(parents=True,exist_ok=True)
    meta_file=meta_dir/f"issue-{issue_number}.json"
    if wt.exists():
        p=git(wt,["status","--porcelain"],check=False)
        if p.returncode == 0:
            current=git(wt,["branch","--show-current"]).stdout.strip()
            if current != branch:
                raise CommandError(f"Worktree {wt} existe con rama {current}, esperaba {branch}")
            if not meta_file.exists():
                raise CommandError(f"Worktree existente sin metadata Autopilot: {meta_file}. Revisar manualmente.")
            meta=json.loads(meta_file.read_text(encoding="utf-8"))
            original_base=meta.get("base_sha")
            if not original_base:
                raise CommandError("Metadata de worktree no contiene base_sha")
            return wt,branch,original_base,False
        raise CommandError(f"Existe {wt} pero no parece un worktree Git válido")
    # Refuse branch collision unless it is already attached to this intended worktree.
    exists=git(repo_path,["show-ref","--verify","--quiet",f"refs/heads/{branch}"],check=False).returncode==0
    if exists:
        raise CommandError(f"La rama local {branch} ya existe. Ejecutá cleanup o revisala manualmente.")
    git(repo_path,["worktree","add",str(wt),"-b",branch,base_sha])
    meta_file.write_text(json.dumps({"issue":issue_number,"branch":branch,"base_sha":base_sha,"worktree":str(wt)},indent=2),encoding="utf-8")
    return wt,branch,base_sha,True


def status_porcelain(worktree: str | Path) -> str:
    return git(worktree,["status","--porcelain=v1","--untracked-files=all"]).stdout


def changed_files(worktree: str | Path, base_sha: str) -> list[str]:
    tracked=git(worktree,["diff","--name-only",base_sha,"--"]).stdout.splitlines()
    untracked=git(worktree,["ls-files","--others","--exclude-standard"]).stdout.splitlines()
    return sorted(set(x.strip().replace("\\","/") for x in tracked+untracked if x.strip()))


def diff_text(worktree: str | Path, base_sha: str, max_chars: int=160000) -> str:
    p=git(worktree,["diff","--no-ext-diff","--unified=3",base_sha,"--"])
    text=p.stdout
    if len(text)>max_chars:
        return text[:max_chars] + f"\n\n[DIFF TRUNCATED: {len(text)-max_chars} chars]\n"
    return text


def ensure_no_protected(changed: list[str], protected: list[str]):
    bad=[]
    for f in changed:
        norm=f.replace("\\","/")
        for p in protected:
            pp=p.replace("\\","/")
            if pp.endswith("/"):
                if norm.startswith(pp): bad.append(norm)
            elif norm == pp or norm.endswith("/"+pp):
                bad.append(norm)
    if bad:
        raise CommandError("Se modificaron paths protegidos: " + ", ".join(sorted(set(bad))))


def commit_all(worktree: str | Path, message: str) -> str:
    git(worktree,["add","-A"])
    # diff --cached --check catches whitespace errors
    git(worktree,["diff","--cached","--check"])
    # The caller already confirmed there are source changes vs base_sha, but
    # those changes may have been committed manually (outside this run)
    # before finalize ran. `git commit` errors on an empty staged diff, so
    # treat "already committed" as success instead of blocking the issue.
    staged = git(worktree,["diff","--cached","--quiet"], check=False)
    if staged.returncode != 0:
        git(worktree,["commit","-m",message])
    return git(worktree,["rev-parse","HEAD"]).stdout.strip()


def push_issue_branch(worktree: str | Path, branch: str):
    git(worktree,["push","-u","origin",branch])


def fast_forward_remote_base(worktree: str | Path, commit_sha: str, base_branch: str, expected_base_sha: str):
    current=remote_branch_sha(str(worktree),base_branch)
    if current != expected_base_sha:
        raise CommandError(f"Base cambió: esperaba {expected_base_sha[:12]}, origin/{base_branch} está en {current[:12]}")
    # commit_sha is expected to descend directly from expected_base_sha because Codex was forbidden to commit.
    parent=git(worktree,["rev-parse",f"{commit_sha}^"]).stdout.strip()
    if parent != expected_base_sha:
        raise CommandError("El commit de issue no tiene como padre directo el SHA base; no es seguro fast-forward automático.")
    git(worktree,["push","origin",f"{commit_sha}:refs/heads/{base_branch}"])
    verify=remote_branch_sha(str(worktree),base_branch)
    if verify != commit_sha:
        raise CommandError("El push a la rama base no quedó en el commit esperado")


def cleanup_worktree(repo_path: str, worktree_root: str, issue_number: int):
    wt=Path(worktree_root)/f"issue-{issue_number}"
    if not wt.exists():
        return False,"No existe worktree"
    dirty=status_porcelain(wt)
    if dirty.strip():
        return False,"Worktree tiene cambios sin commit; no se elimina."
    branch=git(wt,["branch","--show-current"]).stdout.strip()
    git(repo_path,["worktree","remove",str(wt)])
    # Keep branch by default for traceability. User can delete manually later.
    return True,f"Worktree eliminado. Rama preservada: {branch}"


def ensure_baseline_worktree(repo_path: str, root: str, base_sha: str):
    """Create/reuse a detached clean worktree for baseline comparison."""
    fetch(repo_path)
    rootp = Path(root) / "_baseline"
    rootp.mkdir(parents=True, exist_ok=True)
    wt = rootp / base_sha[:12]
    if wt.exists():
        p = git(wt, ["rev-parse", "HEAD"], check=False)
        if p.returncode != 0:
            raise CommandError(f"Baseline worktree inválido: {wt}")
        current = p.stdout.strip()
        if current != base_sha:
            raise CommandError(f"Baseline worktree {wt} apunta a {current[:12]}, esperaba {base_sha[:12]}")
        dirty = status_porcelain(wt)
        if dirty.strip():
            raise CommandError(f"Baseline worktree no está limpio: {wt}")
        return wt, False
    git(repo_path, ["worktree", "add", "--detach", str(wt), base_sha])
    return wt, True


def current_branch(repo: str | Path) -> str:
    return git(repo, ["branch", "--show-current"]).stdout.strip()


def rev_parse(repo: str | Path, ref: str = "HEAD") -> str:
    return git(repo, ["rev-parse", ref]).stdout.strip()


def ref_exists(repo: str | Path, ref: str) -> bool:
    return git(repo, ["show-ref", "--verify", "--quiet", ref], check=False).returncode == 0


def changed_between(repo: str | Path, base: str, head: str = "HEAD") -> list[str]:
    out = git(repo, ["diff", "--name-only", f"{base}..{head}", "--"]).stdout.splitlines()
    return sorted(x.strip().replace("\\", "/") for x in out if x.strip())


def unmerged_files(repo: str | Path) -> list[str]:
    out = git(repo, ["diff", "--name-only", "--diff-filter=U"]).stdout.splitlines()
    return sorted(x.strip().replace("\\", "/") for x in out if x.strip())


def cherry_pick_in_progress(repo: str | Path) -> bool:
    git_dir = Path(git(repo, ["rev-parse", "--git-dir"]).stdout.strip())
    if not git_dir.is_absolute():
        git_dir = Path(repo) / git_dir
    return (git_dir / "CHERRY_PICK_HEAD").exists()


def count_range(repo: str | Path, range_expr: str) -> int:
    out = git(repo, ["rev-list", "--count", range_expr]).stdout.strip()
    return int(out or "0")


def local_base_relation(repo: str | Path, base_branch: str, remote_sha: str) -> tuple[str | None, int, int]:
    """Return local base SHA plus (ahead, behind) relative to the fetched remote SHA."""
    ref = f"refs/heads/{base_branch}"
    if not ref_exists(repo, ref):
        return None, 0, 0
    local = rev_parse(repo, base_branch)
    ahead = count_range(repo, f"{remote_sha}..{local}")
    behind = count_range(repo, f"{local}..{remote_sha}")
    return local, ahead, behind


def find_issue_commit_on_base(repo: str | Path, base_branch: str, issue_number: int) -> str | None:
    # Search the fetched remote history. We use the exact Autopilot commit prefix,
    # not a bare issue number, to avoid false positives from docs/comments.
    p = git(
        repo,
        [
            "log", f"origin/{base_branch}", "--fixed-strings",
            "--grep", f"fix(issue-{issue_number}):",
            "--format=%H", "-n", "1",
        ],
        check=False,
    )
    line = (p.stdout or "").strip().splitlines()
    return line[0] if line else None


def create_detached_worktree(repo_path: str | Path, worktree: str | Path, sha: str) -> Path:
    wt = Path(worktree)
    if wt.exists():
        raise CommandError(f"Ya existe worktree temporal de integración: {wt}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo_path, ["worktree", "add", "--detach", str(wt), sha])
    return wt


def remove_detached_worktree_if_clean(repo_path: str | Path, worktree: str | Path) -> tuple[bool, str]:
    wt = Path(worktree)
    if not wt.exists():
        return True, "worktree temporal ya no existe"
    if cherry_pick_in_progress(wt):
        return False, "cherry-pick en curso; se preserva el worktree"
    dirty = status_porcelain(wt)
    if dirty.strip():
        return False, "worktree temporal tiene cambios; se preserva"
    git(repo_path, ["worktree", "remove", str(wt)])
    return True, "worktree temporal eliminado"


def delete_issue_branch_if_merged(
    repo_path: str | Path,
    worktree_root: str | Path,
    issue_number: int,
    branch: str,
    issue_commit: str,
) -> list[str]:
    """Remove the per-issue worktree and branch (local + remote) once its exact,
    already-integrated commit has been safely landed elsewhere (fast-forward or
    cherry-pick — the caller must have already verified that).

    Never deletes a ref that has moved since: integration can cherry-pick the
    branch onto a new SHA, so an ancestor check against the integrated commit
    would reject every cherry-picked branch. Instead this requires the local
    and remote tips to still be exactly ``issue_commit`` — i.e. nothing new was
    pushed to the branch after we integrated it — which holds for both
    fast-forward and cherry-pick integrations and never touches a dirty worktree.
    """
    messages: list[str] = []
    wt = Path(worktree_root) / f"issue-{issue_number}"
    if wt.exists():
        dirty = status_porcelain(wt)
        if dirty.strip():
            messages.append(f"WARN worktree {wt} tiene cambios sin commit; se preserva junto con la rama {branch}")
            return messages
        current = git(wt, ["branch", "--show-current"], check=False).stdout.strip()
        if current and current != branch:
            messages.append(f"WARN worktree {wt} está en rama inesperada {current}; se preserva")
            return messages
        rm = git(repo_path, ["worktree", "remove", str(wt)], check=False)
        if rm.returncode == 0:
            messages.append(f"Worktree eliminado: {wt}")
        else:
            # On Windows, AV/indexer can briefly hold a handle on a just-created
            # file (e.g. backend/.pytest_cache), making the physical removal fail
            # even though git already unregistered the worktree. This must never
            # raise here: the integration commit is already published at this
            # point, and an exception would surface as if the whole integration
            # had failed. Prune the (possibly half-removed) registration and leave
            # the leftover directory for manual/next-run cleanup instead.
            git(repo_path, ["worktree", "prune"], check=False)
            detail = ((rm.stdout or "") + (rm.stderr or "")).strip()
            messages.append(f"WARN no se pudo eliminar el worktree {wt} (posible archivo bloqueado): {detail}")

    if ref_exists(repo_path, f"refs/heads/{branch}"):
        local_sha = rev_parse(repo_path, branch)
        if local_sha != issue_commit:
            messages.append(f"WARN rama local {branch} avanzó a {local_sha[:12]} (esperaba {issue_commit[:12]}); no se borra")
        else:
            git(repo_path, ["branch", "-D", branch])
            messages.append(f"Rama local eliminada: {branch}")

    remote_p = git(repo_path, ["ls-remote", "origin", f"refs/heads/{branch}"], check=False)
    remote_line = (remote_p.stdout or "").strip()
    if remote_p.returncode == 0 and remote_line:
        remote_sha = remote_line.split()[0]
        if remote_sha != issue_commit:
            messages.append(f"WARN rama remota origin/{branch} avanzó a {remote_sha[:12]} (esperaba {issue_commit[:12]}); no se borra")
        else:
            del_p = git(repo_path, ["push", "origin", "--delete", branch], check=False)
            if del_p.returncode == 0:
                messages.append(f"Rama remota eliminada: origin/{branch}")
            else:
                messages.append(f"WARN no se pudo borrar origin/{branch}: {del_p.stdout}{del_p.stderr}")
    return messages


def push_head_to_base_if_unchanged(worktree: str | Path, base_branch: str, expected_remote_sha: str) -> str:
    current = remote_branch_sha(str(worktree), base_branch)
    if current != expected_remote_sha:
        raise CommandError(
            f"La base remota cambió durante la validación: esperaba {expected_remote_sha[:12]}, "
            f"origin/{base_branch} está en {current[:12]}. No se hizo push."
        )
    head = rev_parse(worktree, "HEAD")
    git(worktree, ["push", "origin", f"HEAD:refs/heads/{base_branch}"])
    verify = remote_branch_sha(str(worktree), base_branch)
    if verify != head:
        raise CommandError(f"Push no verificó el SHA esperado: HEAD={head[:12]} remote={verify[:12]}")
    return head


# ── Release gate ────────────────────────────────────────────────────────────
# "Integrado" y "entregado" no son lo mismo. Cuando la rama de integración y la
# de despliegue son distintas, un merge exitoso a integración no pone el trabajo
# en producción. Estos helpers responden la única pregunta que importa antes de
# cerrar una issue: ¿este trabajo es alcanzable desde la rama de despliegue?


def is_ancestor(repo: str | Path, commit: str, ref: str) -> bool:
    """True si `commit` es alcanzable desde `ref` por historia directa."""
    p = git(repo, ["merge-base", "--is-ancestor", commit, ref], check=False)
    return p.returncode == 0


def cherry_status(repo: str | Path, upstream: str, head: str, limit: str | None = None) -> list[tuple[str, str]]:
    """`git cherry` como lista de (signo, sha).

    El signo es lo que importa:
      ``-`` el parche ya existe upstream (aunque el SHA difiera: cherry-pick,
            rebase, squash con el mismo contenido).
      ``+`` el trabajo todavía no está upstream.
    """
    args = ["cherry", upstream, head]
    if limit:
        args.append(limit)
    p = git(repo, args, check=False)
    if p.returncode != 0:
        return []
    rows: list[tuple[str, str]] = []
    for line in (p.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] in ("+", "-"):
            rows.append((parts[0], parts[1]))
    return rows


def commit_is_released(repo: str | Path, commit: str, deployment_ref: str) -> bool:
    """¿El trabajo de `commit` está en la rama de despliegue?

    Comparar SHAs no alcanza: la promoción puede haber sido por cherry-pick,
    rebase o squash, y entonces el SHA cambia aunque el contenido sea el mismo.
    Por eso se combinan dos señales: ancestro directo, o parche equivalente
    upstream según `git cherry`.
    """
    if is_ancestor(repo, commit, deployment_ref):
        return True
    rows = cherry_status(repo, deployment_ref, commit, f"{commit}^")
    return bool(rows) and all(sign == "-" for sign, _ in rows)


def branch_release_status(repo: str | Path, branch_ref: str, deployment_ref: str) -> dict:
    """Estado de promoción de una rama de issue completa.

    `pending` son los commits con `+`: trabajo propio que todavía no llegó a la
    rama de despliegue. Mientras haya uno, la issue no está entregada.
    """
    rows = cherry_status(repo, deployment_ref, branch_ref)
    pending = [sha for sign, sha in rows if sign == "+"]
    equivalent = [sha for sign, sha in rows if sign == "-"]
    return {
        "branch": branch_ref,
        "deployment": deployment_ref,
        "pending": pending,
        "equivalent": equivalent,
        "released": not pending,
    }


def branch_divergence(repo: str | Path, left_ref: str, right_ref: str) -> tuple[int, int]:
    """(commits exclusivos de left, commits exclusivos de right)."""
    p = git(repo, ["rev-list", "--left-right", "--count", f"{left_ref}...{right_ref}"], check=False)
    if p.returncode != 0:
        return (0, 0)
    parts = (p.stdout or "").split()
    if len(parts) != 2:
        return (0, 0)
    return (int(parts[0]), int(parts[1]))


def find_issue_commits_on_branch(repo: str | Path, branch_ref: str, issue_number: int, limit: int = 50) -> list[str]:
    """Commits de Autopilot para una issue en `branch_ref`, del más viejo al más nuevo.

    Es el respaldo cuando la rama `agent/issue-N-*` ya fue borrada de origin:
    el trabajo sigue identificable por el prefijo de commit que escribe
    Autopilot. Se usa el prefijo exacto, no el número suelto, para no levantar
    menciones en documentación o mensajes de otros commits.
    """
    p = git(
        repo,
        [
            "log", branch_ref, "--fixed-strings",
            "--grep", f"fix(issue-{issue_number}):",
            "--format=%H", "-n", str(limit),
        ],
        check=False,
    )
    if p.returncode != 0:
        return []
    return list(reversed([x.strip() for x in (p.stdout or "").splitlines() if x.strip()]))
