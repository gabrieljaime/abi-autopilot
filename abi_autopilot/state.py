from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStateStore:
    def __init__(self, runtime_dir: Path):
        self.root = runtime_dir / "state"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, issue_number: int) -> Path:
        return self.root / f"issue-{issue_number}.json"

    def load(self, issue_number: int) -> dict | None:
        p = self.path(issue_number)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save(self, issue_number: int, **updates) -> dict:
        data = self.load(issue_number) or {"issue": issue_number, "created_at": _stamp()}
        data.update(updates)
        data["updated_at"] = _stamp()
        self.path(issue_number).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return data

    def clear(self, issue_number: int):
        p = self.path(issue_number)
        if p.exists():
            p.unlink()
