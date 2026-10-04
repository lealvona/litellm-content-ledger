"""Find and replay ledger records across the spool and the store."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import records as rec_mod
from .records import Record, file_hour, ledger_files, parse_ts, session_ids

LONG_CALL_SLACK = timedelta(days=1)


def _in_window(path: Path, since: datetime | None, until: datetime | None) -> bool:
    hour = file_hour(path)
    if hour is None:
        return True
    if since is not None and hour + timedelta(hours=1) <= since:
        return False
    if until is not None and hour - LONG_CALL_SLACK > until:
        return False
    return True


def records(roots: list[Path], since: str | None = None, until: str | None = None) -> Iterator[tuple[Path, Record]]:
    lo, hi = parse_ts(since), parse_ts(until)
    for path in ledger_files(roots):
        if not _in_window(path, lo, hi):
            continue
        for rec in rec_mod.open_records(path):
            ts = parse_ts(rec.get("ts_start"))
            if lo is not None and ts is not None and ts < lo:
                continue
            if hi is not None and ts is not None and ts > hi:
                continue
            yield path, rec


def get(roots: list[Path], ident: str) -> Record | None:
    for _path, rec in records(roots):
        if ident in (rec.get("call_id"), rec.get("id")):
            return rec
    return None


def find(roots: list[Path], since: str | None = None, until: str | None = None, model: str | None = None,
         caller: str | None = None, status: str | None = None, text: str | None = None,
         limit: int | None = None) -> list[Record]:
    needle = text.lower() if text else None
    out: list[Record] = []
    for _path, rec in records(roots, since, until):
        if model and model not in (rec.get("model"), rec.get("model_group")):
            continue
        if caller and caller != (rec.get("caller") or {}).get("key_alias"):
            continue
        if status and status != rec.get("status"):
            continue
        if needle and needle not in json.dumps(rec.get("messages"), ensure_ascii=False).lower() \
                and needle not in json.dumps(rec.get("response"), ensure_ascii=False).lower():
            continue
        out.append(rec)
        if limit is not None and len(out) >= limit:
            break
    return out


def trace(roots: list[Path], session: str) -> list[Record]:
    hits = [rec for _path, rec in records(roots) if session in session_ids(rec)]
    return sorted(hits, key=lambda r: r.get("ts_start") or "")


def stats(roots: list[Path], days: int | None = None) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else None
    files = ledger_files(roots)
    by_day: dict[str, dict[str, int]] = {}
    count = 0
    for _path, rec in records(roots, since):
        day = (rec.get("ts_start") or "unknown")[:10]
        slot = by_day.setdefault(day, {"records": 0, "prompt_tokens": 0, "completion_tokens": 0})
        slot["records"] += 1
        usage = rec.get("usage") or {}
        slot["prompt_tokens"] += usage.get("prompt_tokens") or 0
        slot["completion_tokens"] += usage.get("completion_tokens") or 0
        count += 1
    return {"records": count, "files": len(files), "bytes_on_disk": sum(p.stat().st_size for p in files),
            "by_day": dict(sorted(by_day.items()))}


def summary(rec: Record) -> str:
    usage = rec.get("usage") or {}
    return (f"{(rec.get('ts_start') or '?')[:19]}  {rec.get('status', '?'):7}  {rec.get('call_id')}  "
            f"{rec.get('model_group') or rec.get('model')}  {(rec.get('caller') or {}).get('key_alias')}  "
            f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}  trace={rec.get('trace_id')}")
