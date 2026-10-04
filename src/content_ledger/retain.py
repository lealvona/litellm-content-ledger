"""Retention: delete day directories older than max_age_days, then oldest-first until under max_bytes.
Today's directory is never deleted. Every deletion is logged in the store's _ops/retention.log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .store import DayUsage, Store


@dataclass(frozen=True, slots=True)
class Deletion:
    usage: DayUsage
    reason: str


def plan_deletions(days: list[DayUsage], today: date, max_age_days: int, cap_bytes: int) -> list[Deletion]:
    ordered = sorted(days, key=lambda d: d.day)
    by_age = [Deletion(d, "age") for d in ordered if (today - d.day).days > max_age_days and d.day != today]
    aged = {d.usage.day for d in by_age}
    remaining = [d for d in ordered if d.day not in aged]
    total = sum(d.bytes for d in remaining)
    by_size = []
    for d in remaining:
        if total <= cap_bytes or d.day == today:
            break
        by_size.append(Deletion(d, "size"))
        total -= d.bytes
    return by_age + by_size


def apply(store: Store, plan: list[Deletion], now: datetime) -> list[str]:
    lines = []
    for deletion in plan:
        line = (f"{now.isoformat()} deleted {deletion.usage.rel_path} bytes={deletion.usage.bytes} "
                f"reason={deletion.reason}")
        store.delete_day(deletion.usage.rel_path, line)
        print(line, flush=True)
        lines.append(line)
    return lines
