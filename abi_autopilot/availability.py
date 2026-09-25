# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class AvailabilitySignal:
    kind: str  # quota | transient | fatal
    retry_after_seconds: int | None = None
    reason: str = ""


_QUOTA_PATTERNS = [
    r"usage limit",
    r"quota exceeded",
    r"exceeded your current quota",
    r"out of credits",
    r"insufficient credits",
    r"credit balance",
    r"you(?:'|’)ve hit .*limit",
    r"you have hit .*limit",
    r"limit reached.*reset",
    r"resets? (?:at|in|after)",
    r"use .* again after",
]

_TRANSIENT_PATTERNS = [
    r"rate limit",
    r"too many requests",
    r"\b429\b",
    r"temporar(?:y|ily) unavailable",
    r"service unavailable",
    r"overloaded",
    r"\b502\b",
    r"\b503\b",
    r"\b504\b",
    r"connection reset",
    r"connection aborted",
    r"timed? out",
    r"timeout",
]


def parse_retry_after_seconds(text: str, now: datetime | None = None) -> int | None:
    if not text:
        return None
    t = text.lower()

    # "retry in 2 hours 15 minutes" / "resets after 15 minutes"
    m = re.search(r"(?:retry|try again|resets?)\s+(?:after|in)\s+([^\n\r.]+)", t)
    if m:
        chunk = m.group(1)
        total = 0
        found = False
        for n, unit in re.findall(r"(\d+)\s*(second|minute|hour)s?", chunk):
            total += int(n) * {"second": 1, "minute": 60, "hour": 3600}[unit]
            found = True
        if found:
            return total

    # ISO-like timestamp.
    m = re.search(r"(20\d\d-\d\d-\d\d[t ]\d\d:\d\d(?::\d\d)?(?:z|[+-]\d\d:?\d\d)?)", text, re.I)
    if m:
        raw = m.group(1).replace(" ", "T")
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            target = datetime.fromisoformat(raw)
            current = now or datetime.now().astimezone()
            if target.tzinfo is None:
                target = target.replace(tzinfo=current.tzinfo)
            return max(0, int((target - current).total_seconds()))
        except ValueError:
            pass

    # Local clock "resets at 3:45 PM".
    m = re.search(r"(?:resets?|again after)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m:
        current = now or datetime.now().astimezone()
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm:
            if hour == 12:
                hour = 0
            if ampm == "pm":
                hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= current:
                target += timedelta(days=1)
            return int((target - current).total_seconds())

    return None


def classify_provider_failure(text: str, now: datetime | None = None) -> AvailabilitySignal:
    t = (text or "").lower()
    if any(re.search(p, t, re.I) for p in _QUOTA_PATTERNS):
        return AvailabilitySignal("quota", parse_retry_after_seconds(text, now), "provider quota/usage limit")
    if any(re.search(p, t, re.I) for p in _TRANSIENT_PATTERNS):
        return AvailabilitySignal("transient", parse_retry_after_seconds(text, now), "temporary provider/rate failure")
    return AvailabilitySignal("fatal", None, "agent returned a non-retryable error")
