# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
"""Cross-process locks for resources shared by parallel issue runs.

With `autopilot.max_parallel > 1` several `run` processes work on the same
repository at once. Git operations that touch shared state (fetch, worktree
add, pushes that update remote-tracking refs) and the per-commit baseline
worktree must not run concurrently. A lock is a directory created atomically
with `mkdir`; it works on every OS without extra dependencies.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    pass


@contextmanager
def file_lock(root: str | Path, name: str, timeout: float = 1800, stale_seconds: float = 7200, poll: float = 0.5):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock_dir = root / (re.sub(r"[^A-Za-z0-9_.-]+", "-", name) + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            try:
                age = time.time() - lock_dir.stat().st_mtime
            except FileNotFoundError:
                continue
            # A crashed process never releases its lock; reclaim it when old.
            if age > stale_seconds:
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(f"Timed out waiting for lock {lock_dir}")
            time.sleep(poll)
    try:
        (lock_dir / "owner.json").write_text(json.dumps({"pid": os.getpid(), "since": time.time()}), encoding="utf-8")
        yield lock_dir
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)
