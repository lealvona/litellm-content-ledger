"""Export redacted records to other systems; import records from other systems into the spool."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .records import parse_ts, validate
from .redact import Redactor
from .search import records as search_records


def redacted_records(roots: list[Path], redactor: Redactor, since: str | None, until: str | None
                     ) -> Iterator[dict[str, Any]]:
    """Spool records are not yet redacted, store records are; redacting again is harmless, so everything
    handed to an exporter goes through the redactor."""
    for _path, rec in search_records(roots, since, until):
        clean, _n = redactor.record(rec)
        yield clean


def export(roots: list[Path], redactor: Redactor, exporters: list[Any], since: str | None = None,
           until: str | None = None) -> dict[str, int]:
    results: dict[str, int] = {}
    for exporter in exporters:
        name = type(exporter).__name__
        try:
            results[name] = exporter.export(redacted_records(roots, redactor, since, until))
        finally:
            close = getattr(exporter, "close", None)
            if callable(close):
                close()
    return results


@dataclass(slots=True)
class ImportReport:
    imported: int = 0
    rejected: int = 0
    problems: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def import_into_spool(source: Iterable[Any], spool: Path, source_name: str) -> ImportReport:
    """Validate each record and append it to spool/<day>/<HH>.import.<tag>-0.jsonl by its ts_start hour.
    The normal ship run then redacts, compresses and stores it."""
    report = ImportReport()
    tag = hashlib.sha256(f"{source_name}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:10]
    stamp = datetime.now(timezone.utc).isoformat()
    handles: dict[Path, Any] = {}
    try:
        for i, rec in enumerate(source):
            problems = validate(rec)
            if problems:
                report.rejected += 1
                if len(report.problems) < 20:
                    report.problems.append(f"record {i}: {'; '.join(problems)}")
                continue
            ts = parse_ts(rec["ts_start"]).astimezone(timezone.utc)
            day_dir = spool / ts.strftime("%Y-%m-%d")
            if not spool.is_dir():
                spool.mkdir(mode=0o700, parents=True)
            day_dir.mkdir(mode=0o700, exist_ok=True)
            path = day_dir / f"{ts:%H}.import.{tag}-0.jsonl"
            if path not in handles:
                handles[path] = open(path, "a", encoding="utf-8")
                report.files.append(str(path))
            out = dict(rec)
            out["imported"] = {"source": source_name, "at": stamp}
            handles[path].write(json.dumps(out, ensure_ascii=False) + "\n")
            report.imported += 1
    finally:
        for fh in handles.values():
            fh.close()
    return report
