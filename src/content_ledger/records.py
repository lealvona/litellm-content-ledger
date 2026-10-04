"""Reading and validating ledger records in the spool and store layouts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RECORD_VERSION = 1
SPOOL_FILE = re.compile(r"(\d{4})-(\d{2})-(\d{2})/(\d{2})\.[^/]+\.jsonl$")
STORE_FILE = re.compile(r"(\d{4})/(\d{2})/(\d{2})/(\d{2})\.[^/]+\.jsonl(?:\.zst)?$")

Record = dict[str, Any]


def zstd_binary() -> str:
    explicit = os.environ.get("ZSTD")
    if explicit:
        return explicit
    found = shutil.which("zstd")
    if not found:
        raise RuntimeError("zstd not found on PATH; install it or set ZSTD / [compress] zstd in the profile")
    return found


def file_hour(path: Path) -> datetime | None:
    s = path.as_posix()
    m = SPOOL_FILE.search(s) or STORE_FILE.search(s)
    if not m:
        return None
    y, mo, d, h = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, tzinfo=timezone.utc)


def ledger_files(roots: list[Path]) -> list[Path]:
    files = [p for r in roots if r.is_dir() for p in r.rglob("*.jsonl*") if file_hour(p) is not None]
    return sorted(files, key=lambda p: (file_hour(p), p.name))


def open_records(path: Path, zstd: str | None = None) -> Iterator[Record]:
    """The one place files are opened. Encryption-at-rest would add its decrypt step here."""
    if path.name.endswith(".zst"):
        text = subprocess.run(
            [zstd or zstd_binary(), "-dc", str(path)], capture_output=True, check=True, stdin=subprocess.DEVNULL
        ).stdout.decode("utf-8", errors="replace")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def validate(record: Any) -> list[str]:
    """Problems that stop a record from being stored; empty when it is a usable schema-v1 record."""
    if not isinstance(record, Mapping):
        return ["not a JSON object"]
    problems = []
    if record.get("v") != RECORD_VERSION:
        problems.append(f"v must be {RECORD_VERSION}")
    if parse_ts(record.get("ts_start")) is None:
        problems.append("ts_start must be an ISO-8601 timestamp")
    if not (record.get("call_id") or record.get("id")):
        problems.append("call_id or id is required")
    if record.get("status") not in ("success", "failure"):
        problems.append("status must be success or failure")
    return problems


def session_ids(rec: Mapping[str, Any]) -> set[str]:
    headers = (rec.get("correlation") or {}).get("headers") or {}
    return {v for v in (rec.get("trace_id"), headers.get("x-litellm-session-id"), headers.get("x-litellm-trace-id")) if v}
