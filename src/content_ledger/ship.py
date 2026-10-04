"""Ship closed hourly spool files to the store, verified, then clear the spool copy.

Per closed hour file:
    redact (Redactor, its own process)  ->  compress (zstd)  ->  [encrypt: hook, see README]  ->  upload
    ->  sha256 at the far end == local?   yes: delete spool copy   no: keep it, report, retry next run
A different file of the same name already in the store is never overwritten; the new one gets a hash suffix.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .redact import Redactor
from .store import Store, sha256_file

MIN_IDLE_S = 120
SPOOL_NAME = re.compile(r"^(\d{2})\.[A-Za-z0-9_.-]+-\d+\.jsonl$")
DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Compressor = Callable[[Path, Path], None]


@dataclass(slots=True)
class ShipReport:
    shipped: int = 0
    failed: int = 0
    bytes_raw: int = 0
    bytes_compressed: int = 0
    redactions: int = 0
    unparseable_lines: int = 0
    failures: list[str] = field(default_factory=list)


def hour_start(path: Path) -> datetime | None:
    m = SPOOL_NAME.match(path.name)
    if not m or not DAY_DIR.match(path.parent.name):
        return None
    return datetime.strptime(f"{path.parent.name} {m.group(1)}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc)


def closed_files(spool: Path, now: datetime, grace: timedelta) -> list[Path]:
    out = []
    for path in sorted(spool.glob("*/*.jsonl")):
        start = hour_start(path)
        if start is None or start + timedelta(hours=1) + grace > now:
            continue
        if now.timestamp() - path.stat().st_mtime < MIN_IDLE_S:
            continue
        out.append(path)
    return out


def zstd_compressor(zstd: str, level: int) -> Compressor:
    def compress(src: Path, out: Path) -> None:
        subprocess.run([zstd, f"-{level}", "-q", "-f", "-o", str(out), str(src)], check=True,
                       stdin=subprocess.DEVNULL)
    return compress


def redact_file(src: Path, out: Path, redactor: Redactor) -> tuple[int, int]:
    """Returns (redactions, unparseable lines). A line that was valid JSON must stay valid after redaction,
    otherwise ValueError (the file is kept and the failure reported). A line that was never valid JSON
    (a torn write) is redacted without the check and counted."""
    total = bad = 0
    with open(src, encoding="utf-8", errors="replace") as fin, open(out, "w", encoding="utf-8") as fout:
        for line in fin:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                clean, n = redactor.line(line)
                bad += 1
            else:
                clean, n = redactor.line_checked(line)
            fout.write(clean if clean.endswith("\n") else clean + "\n")
            total += n
    return total, bad


def _ship_one(path: Path, store: Store, compress: Compressor, redactor: Redactor, report: ShipReport) -> None:
    raw = path.stat().st_size
    redacted = path.with_name(path.name + ".redacted")
    packed = path.with_name(path.name + ".zst")
    try:
        redactions, bad = redact_file(path, redacted, redactor)
        compress(redacted, packed)
    finally:
        redacted.unlink(missing_ok=True)
    local_hash = sha256_file(packed)
    day = path.parent.name.replace("-", "/")
    rel = f"{day}/{packed.name}"
    existing = store.sha256(rel)
    if existing is not None and existing != local_hash:
        rel = f"{day}/{path.name[: -len('.jsonl')]}.{local_hash[:12]}.jsonl.zst"
        existing = store.sha256(rel)
    if existing != local_hash:
        store.upload(packed, rel)
    far_end = store.sha256(rel)
    if far_end != local_hash:
        msg = f"{path.name} -> {rel}: local sha256 {local_hash}, far end {far_end}; spool copy kept"
        print(f"FAIL {msg}", flush=True)
        report.failed += 1
        report.failures.append(msg)
        return
    size = packed.stat().st_size
    path.unlink()
    packed.unlink()
    print(f"shipped {path.name} ({raw} B raw, {redactions} redactions, {size} B zst) -> {rel} sha256 {local_hash}; "
          f"spool copy removed", flush=True)
    report.shipped += 1
    report.bytes_raw += raw
    report.bytes_compressed += size
    report.redactions += redactions
    report.unparseable_lines += bad


def ship(spool: Path, store: Store, now: datetime, compress: Compressor, redactor: Redactor,
         grace: timedelta = timedelta(minutes=10)) -> ShipReport:
    report = ShipReport()
    for path in closed_files(spool, now, grace):
        try:
            _ship_one(path, store, compress, redactor, report)
        except Exception as exc:
            msg = f"{path.name}: {type(exc).__name__}: {exc}; spool copy kept"
            print(f"FAIL {msg}", flush=True)
            report.failed += 1
            report.failures.append(msg)
    today = now.strftime("%Y-%m-%d")
    for day_dir in spool.glob("*"):
        if day_dir.is_dir() and DAY_DIR.match(day_dir.name) and day_dir.name != today and not any(day_dir.iterdir()):
            day_dir.rmdir()
    return report
