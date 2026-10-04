"""Health events for notifiers. Events carry counters and sizes, never content.

Sources:
- spool/_status/<host>-<pid>.json, written by the proxy-side writer: drop and error counters.
- spool size against the profile's spool cap.
- closed hour files that should have shipped already.
- ship and retain results, passed in by those commands.
State in spool/_state/health.json keeps a condition from being re-reported every run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .profile import SEVERITIES
from .records import ledger_files
from .ship import closed_files

Event = dict[str, Any]
WATCHED = ("dropped_queue", "dropped_spool", "errors", "build_errors")
STALE_AFTER = timedelta(hours=2)


def event(kind: str, severity: str, summary: str, **attrs: Any) -> Event:
    return {"kind": kind, "severity": severity, "summary": summary, "attrs": attrs}


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def spool_bytes(spool: Path) -> int:
    return sum(p.stat().st_size for p in ledger_files([spool]))


def check(spool: Path, spool_cap_bytes: int, now: datetime, grace: timedelta) -> list[Event]:
    state_path = spool / "_state" / "health.json"
    state = _load_state(state_path)
    seen: dict[str, dict[str, int]] = state.get("counters", {})
    events: list[Event] = []

    for status_file in sorted((spool / "_status").glob("*.json")):
        try:
            status = json.loads(status_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        tag = str(status.get("tag", status_file.stem))
        before = seen.get(tag, {})
        delta = {k: int(status.get(k, 0)) - int(before.get(k, 0)) for k in WATCHED}
        if before and any(v < 0 for v in delta.values()):
            delta = {k: int(status.get(k, 0)) for k in WATCHED}
        lost = delta["dropped_queue"] + delta["dropped_spool"]
        if lost > 0 or delta["errors"] > 0 or delta["build_errors"] > 0:
            severity = "critical" if delta["errors"] > 0 or delta["dropped_spool"] > 0 else "warning"
            events.append(event("records_dropped", severity,
                                 f"{lost} record(s) dropped, {delta['errors']} write error(s), "
                                 f"{delta['build_errors']} build error(s) in writer {tag}",
                                 writer=tag, **delta))
        seen[tag] = {k: int(status.get(k, 0)) for k in WATCHED}

    used = spool_bytes(spool)
    level = "full" if used >= 0.95 * spool_cap_bytes else "high" if used >= 0.8 * spool_cap_bytes else "ok"
    if level != state.get("spool_level", "ok") and level != "ok":
        events.append(event("spool_high", "critical" if level == "full" else "warning",
                            f"spool at {used} of {spool_cap_bytes} bytes ({100 * used // max(spool_cap_bytes, 1)}%)",
                            spool_bytes=used, spool_cap_bytes=spool_cap_bytes))

    overdue = [p for p in closed_files(spool, now, grace) if now.timestamp() - p.stat().st_mtime > STALE_AFTER.total_seconds()]
    stale = bool(overdue)
    if stale and not state.get("stale", False):
        events.append(event("ship_stale", "warning", f"{len(overdue)} closed spool file(s) unshipped for over 2 h",
                            files=len(overdue)))

    _save_state(state_path, {"counters": seen, "spool_level": level, "stale": stale})
    return events


def dispatch(events: Iterable[Event], notifiers: list[Any], min_severity: str) -> int:
    """Send events at or above min_severity to every notifier. A failing notifier is reported, never fatal."""
    floor = SEVERITIES.index(min_severity)
    sent = 0
    for ev in events:
        if SEVERITIES.index(ev["severity"]) < floor:
            continue
        for notifier in notifiers:
            try:
                notifier.notify(ev)
                sent += 1
            except Exception as exc:
                print(f"notifier {type(notifier).__name__} failed: {type(exc).__name__}: {exc}", flush=True)
    return sent
