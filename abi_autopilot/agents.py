# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
from __future__ import annotations
import json
import re
from pathlib import Path
from .shell import run_capture, CommandError


BUNDLED_PROMPTS = Path(__file__).resolve().parent / "prompts"


def _load_prompt(base_dir: Path, name: str, cfg: dict | None = None) -> str:
    # A copy in <home>/prompts/ overrides the prompt shipped with the package.
    override = base_dir / "prompts" / name
    text = (override if override.is_file() else BUNDLED_PROMPTS / name).read_text(encoding="utf-8")
    protected = (cfg or {}).get("protected_paths") or []
    listed = ", ".join(f"`{p}`" for p in protected) if protected else "(none configured)"
    return text.replace("{{PROTECTED_PATHS}}", listed)


def build_issue_context(issue, base_sha: str) -> str:
    return f"""\nISSUE LIVE\nNumber: #{issue.number}\nTitle: {issue.title}\nURL: {issue.url}\nRisk: {issue.risk}\nBase SHA: {base_sha}\n\nBODY\n{issue.body}\n"""


def run_codex(base_dir: Path, cfg: dict, worktree: Path, issue, base_sha: str, run_dir: Path,
              mode: str="implement", feedback: str | None=None) -> tuple[int,str,str]:
    codex=cfg["codex"]
    template=_load_prompt(base_dir,"fixer.md" if mode=="fix" else "implementer.md",cfg)
    prompt=template + "\n" + build_issue_context(issue,base_sha)
    if feedback:
        prompt += "\n\nBLOCKERS / TEST FAILURES TO FIX\n" + feedback[-60000:]
    (run_dir/f"codex-{mode}-prompt.txt").write_text(prompt,encoding="utf-8")
    args=[codex.get("command","codex"),"exec","--sandbox",codex.get("sandbox","workspace-write")]
    model=codex.get("fix_model") if mode=="fix" and codex.get("fix_model") else codex.get("model")
    if model:
        args += ["-m",model]
    args += list(codex.get("extra_args") or [])
    args += ["--json","-"]
    p=run_capture(args,cwd=worktree,input_text=prompt,timeout=7200,check=False)
    (run_dir/f"codex-{mode}.jsonl").write_text(p.stdout or "",encoding="utf-8")
    (run_dir/f"codex-{mode}.stderr.txt").write_text(p.stderr or "",encoding="utf-8")
    return p.returncode,p.stdout or "",p.stderr or ""


def _claude_implement_args(claude: dict, *, max_turns: int) -> list[str]:
    # Same stdin-only rationale as _claude_review_args: the prompt travels over
    # stdin, never argv, to avoid Windows' CreateProcess command-line limit.
    args = [claude.get("command", "claude"), "-p"]
    args += [
        "Implement the requested changes on stdin directly in the working tree using your file-edit tools.",
        "--input-format", "text",
        "--output-format", "json",
        "--max-turns", str(max_turns),
    ]
    model = claude.get("implement_model") or claude.get("model")
    if model:
        args += ["--model", model]
    # Unlike the reviewer (read-only, "plan"), the implementer must be able to
    # write files without an interactive prompt — this CLI call is headless.
    permission_mode = claude.get("implement_permission_mode", "acceptEdits")
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    return args


def run_claude_implement(base_dir: Path, cfg: dict, worktree: Path, issue, base_sha: str, run_dir: Path,
                          mode: str = "implement", feedback: str | None = None) -> tuple[int, str, str]:
    claude = cfg["claude"]
    template = _load_prompt(base_dir, "fixer.md" if mode == "fix" else "implementer.md", cfg)
    prompt = template + "\n" + build_issue_context(issue, base_sha)
    if feedback:
        prompt += "\n\nBLOCKERS / TEST FAILURES TO FIX\n" + feedback[-60000:]
    (run_dir/f"claude-implement-{mode}-prompt.txt").write_text(prompt, encoding="utf-8")
    max_turns = int(claude.get("implement_max_turns") or claude.get("max_turns", 8))
    args = _claude_implement_args(claude, max_turns=max_turns)
    p = run_capture(args, cwd=worktree, input_text=prompt, timeout=7200, check=False)
    (run_dir/f"claude-implement-{mode}.json").write_text(p.stdout or "", encoding="utf-8")
    (run_dir/f"claude-implement-{mode}.stderr.txt").write_text(p.stderr or "", encoding="utf-8")
    return p.returncode, p.stdout or "", p.stderr or ""


def _extract_json_object(text: str) -> dict:
    t=text.strip()
    if t.startswith("```"):
        t=re.sub(r"^```(?:json)?\s*","",t)
        t=re.sub(r"\s*```$","",t)
    try:
        return json.loads(t)
    except Exception:
        m=re.search(r"\{.*\}",t,re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _claude_outer(stdout: str) -> dict:
    try:
        outer = json.loads(stdout or "{}")
    except Exception as exc:
        raise CommandError(f"Claude returned invalid outer JSON: {exc}", output=stdout or "")
    if not isinstance(outer, dict):
        raise CommandError("Claude returned a non-object outer response", output=stdout or "")
    return outer


def _claude_hit_max_turns(outer: dict) -> bool:
    return bool(
        outer.get("subtype") == "error_max_turns"
        or outer.get("terminal_reason") == "max_turns"
        or (outer.get("is_error") and "maximum number of turns" in " ".join(outer.get("errors") or []).lower())
    )


def _extract_review_from_outer(outer: dict) -> dict:
    # Claude Code --json-schema returns validated data in structured_output.
    # Keep result parsing as a compatibility fallback for older CLI versions.
    structured = outer.get("structured_output")
    if isinstance(structured, dict):
        review = structured
    else:
        result = outer.get("result", "")
        if not isinstance(result, str) or not result.strip():
            raise CommandError("Reviewer returned neither structured_output nor a usable result field", output=json.dumps(outer, ensure_ascii=False))
        review = _extract_json_object(result)
    if review.get("verdict") not in ("PASS", "FAIL"):
        raise CommandError("Reviewer did not return a PASS/FAIL verdict", output=json.dumps(review, ensure_ascii=False))
    return review


def _candidate_diff_context(worktree: Path, base_sha: str, max_chars: int) -> str:
    # Supplying deterministic Git evidence up-front keeps the reviewer focused and
    # avoids spending agentic turns rediscovering the same diff with tools.
    chunks: list[str] = []
    for title, args in (
        ("GIT STATUS", ["git", "status", "--short"]),
        ("CHANGED FILES", ["git", "diff", "--name-status", base_sha]),
        ("DIFF STAT", ["git", "diff", "--stat", base_sha]),
        ("PATCH", ["git", "diff", "--no-ext-diff", "--unified=5", base_sha, "--"]),
    ):
        p = run_capture(args, cwd=worktree, timeout=120, check=False)
        text = (p.stdout or "") + (("\nSTDERR:\n" + p.stderr) if p.stderr else "")
        chunks.append(f"\n## {title}\n{text}")
    joined = "".join(chunks)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + f"\n\n[PATCH TRUNCATED at {max_chars} chars; use read-only tools only if a blocker cannot be decided from this evidence.]"
    return joined


def _review_json_schema() -> str:
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
            "summary": {"type": "string"},
            "blocking": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "evidence": {"type": "string"},
                        "file": {"type": ["string", "null"]},
                        "required_fix": {"type": "string"},
                    },
                    "required": ["criterion", "evidence", "required_fix"],
                    "additionalProperties": True,
                },
            },
            "non_blocking": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
        },
        "required": ["verdict", "summary", "blocking", "non_blocking"],
        "additionalProperties": True,
    }
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=False)


def _claude_review_args(claude: dict, *, max_turns: int, resume_session: str | None = None) -> list[str]:
    # IMPORTANT (Windows): never put the large review prompt on argv. Windows CreateProcess
    # has a command-line length limit and Python raises WinError 206 when the diff/prompt
    # is large. The full context is sent over stdin by run_claude_review instead.
    args = [claude.get("command", "claude"), "-p"]
    if resume_session:
        args += ["--resume", resume_session]
    args += [
        "Review the context provided on stdin and return the requested verdict.",
        "--input-format", "text",
        "--output-format", "json",
        "--json-schema", _review_json_schema(),
        "--max-turns", str(max_turns),
    ]
    if not resume_session and claude.get("model"):
        args += ["--model", claude["model"]]
    if claude.get("permission_mode"):
        args += ["--permission-mode", claude["permission_mode"]]
    return args


def run_claude_review(base_dir: Path, cfg: dict, worktree: Path, issue, base_sha: str,
                      validation_summary: str, run_dir: Path) -> dict:
    claude = cfg["claude"]
    template = _load_prompt(base_dir, "reviewer.md", cfg)
    max_diff_chars = int(claude.get("max_diff_chars", 120000))
    diff_context = _candidate_diff_context(worktree, base_sha, max_diff_chars)
    prompt = template + "\n" + build_issue_context(issue, base_sha)
    prompt += f"\n\nVALIDATION SO FAR\n{validation_summary}\n"
    prompt += "\nDETERMINISTIC GIT EVIDENCE (already collected; do not rediscover broadly)\n" + diff_context
    prompt += """

REVIEW EXECUTION BUDGET
- No modifiques archivos.
- No vuelvas a correr suites completas: ya tenés la evidencia de validación arriba.
- No explores el repo de forma amplia. Concentrate en los archivos cambiados y, sólo si es imprescindible para decidir un blocker, en un call-site o contrato directo.
- Priorizá criterios de aceptación de la issue y regresiones introducidas por este diff.
- Terminá con el JSON contractual antes de agotar los turnos.
"""
    (run_dir / "review-prompt.txt").write_text(prompt, encoding="utf-8")

    max_turns = int(claude.get("max_turns", 8))
    resume_turns = int(claude.get("resume_turns", 4))
    max_resumes = int(claude.get("max_turn_resumes", 2))

    session_id: str | None = None
    current_prompt = prompt
    attempts = 0
    while True:
        args = _claude_review_args(
            claude,
            max_turns=max_turns if attempts == 0 else resume_turns,
            resume_session=session_id if attempts > 0 else None,
        )
        # The large prompt/diff travels through stdin, not argv. Besides avoiding
        # WinError 206, this matches Claude Code's documented piped-content mode.
        p = run_capture(args, cwd=worktree, input_text=current_prompt, timeout=7200, check=False)
        suffix = "" if attempts == 0 else f"-resume-{attempts}"
        (run_dir / f"claude-review-raw{suffix}.json").write_text(p.stdout or "", encoding="utf-8")
        (run_dir / f"claude-review-stderr{suffix}.txt").write_text(p.stderr or "", encoding="utf-8")

        outer = _claude_outer(p.stdout or "{}")
        if not outer.get("is_error") and p.returncode == 0:
            review = _extract_review_from_outer(outer)
            (run_dir / "review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")
            return review

        if _claude_hit_max_turns(outer):
            session_id = str(outer.get("session_id") or session_id or "").strip() or None
            if not session_id:
                raise CommandError("Claude hit max_turns but returned no session_id to resume", p.returncode, (p.stdout or "") + "\n" + (p.stderr or ""))
            if attempts >= max_resumes:
                raise CommandError(
                    f"Claude reviewer ran out of max_turns even after {max_resumes} resumes",
                    p.returncode,
                    (p.stdout or "") + "\n" + (p.stderr or ""),
                )
            attempts += 1
            current_prompt = """Llegaste al límite de turnos durante una revisión que ya estaba en curso.
NO reinicies la investigación y NO vuelvas a recorrer el repositorio.
Usá la evidencia y notas que ya reuniste en esta misma sesión.
Si falta algo realmente bloqueante, hacé como máximo una verificación read-only puntual.
Ahora cerrá la revisión y devolvé EXCLUSIVAMENTE el objeto JSON contractual PASS/FAIL, sin markdown ni explicación fuera del JSON.
"""
            print(f"\nCLAUDE REVIEW · max_turns reached; resuming the same session ({attempts}/{max_resumes}) with {resume_turns} turns. Does not count as a review/fix loop.")
            continue

        # Quota / transient failures are classified by the caller from this raw output.
        raise CommandError("Claude reviewer failed", p.returncode, (p.stdout or "") + "\n" + (p.stderr or ""))

