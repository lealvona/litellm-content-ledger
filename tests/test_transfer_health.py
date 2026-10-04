import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from content_ledger import health, transfer
from content_ledger.components import JsonlExporter, JsonlImporter
from content_ledger.redact import Redactor
from content_ledger.ship import ship
from content_ledger.store import LocalStore
from fixtures import rec, write_jsonl

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def test_export_is_always_redacted(tmp_path: Path):
    spool = tmp_path / "spool"
    write_jsonl(spool / "2026-10-03" / "08.h-1.jsonl",
                [rec("c1", "2026-10-03T08:00:00+00:00", "s", text="token Bearer abcdefghijklmnop")])
    out = tmp_path / "out.jsonl"
    result = transfer.export([spool], Redactor(), [JsonlExporter(str(out))])
    assert result == {"JsonlExporter": 1}
    exported = out.read_text()
    assert "abcdefghijklmnop" not in exported and "[REDACTED:bearer]" in exported


def test_import_validates_and_places_by_hour(tmp_path: Path):
    src = tmp_path / "in.jsonl"
    good = rec("c1", "2026-10-02T23:10:00+00:00", "s")
    write_jsonl(src, [good, {"v": 1, "status": "success"}, rec("c2", "2026-10-03T01:00:00+00:00", "s")])
    report = transfer.import_into_spool(JsonlImporter(str(src)).records(), tmp_path / "spool", "jsonl:in")
    assert (report.imported, report.rejected) == (2, 1)
    assert "ts_start" in report.problems[0] and "call_id" in report.problems[0]
    files = sorted(p.relative_to(tmp_path / "spool").as_posix() for p in (tmp_path / "spool").rglob("*.jsonl"))
    assert files[0].startswith("2026-10-02/23.import.") and files[1].startswith("2026-10-03/01.import.")
    first = json.loads((tmp_path / "spool" / files[0]).read_text())
    assert first["imported"]["source"] == "jsonl:in" and first["call_id"] == "c1"


def test_imported_records_ship_through_the_normal_path(tmp_path: Path):
    src = tmp_path / "in.jsonl"
    write_jsonl(src, [rec("c1", "2026-10-02T23:10:00+00:00", "s", text="password=hunter2hunter2")])
    transfer.import_into_spool(JsonlImporter(str(src)).records(), tmp_path / "spool", "jsonl:in")
    for f in (tmp_path / "spool").rglob("*.jsonl"):
        os.utime(f, (NOW.timestamp() - 600, NOW.timestamp() - 600))
    report = ship(tmp_path / "spool", LocalStore(str(tmp_path / "store")), NOW,
                  lambda s, o: o.write_bytes(s.read_bytes()), Redactor())
    assert report.shipped == 1 and report.redactions == 1
    stored = next((tmp_path / "store").rglob("*.zst")).read_text()
    assert "hunter2hunter2" not in stored


def _status(spool: Path, tag: str, **counters) -> None:
    d = spool / "_status"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tag}.json").write_text(json.dumps({"tag": tag, **counters}))


def test_health_reports_drop_deltas_once(tmp_path: Path):
    spool = tmp_path / "spool"
    _status(spool, "h-1", dropped_queue=0, dropped_spool=0, errors=0, build_errors=0)
    assert health.check(spool, 1 << 30, NOW, timedelta(minutes=10)) == []
    _status(spool, "h-1", dropped_queue=3, dropped_spool=0, errors=0, build_errors=0)
    [ev] = health.check(spool, 1 << 30, NOW, timedelta(minutes=10))
    assert (ev["kind"], ev["severity"], ev["attrs"]["dropped_queue"]) == ("records_dropped", "warning", 3)
    assert health.check(spool, 1 << 30, NOW, timedelta(minutes=10)) == []


def test_health_restarted_writer_counts_from_zero(tmp_path: Path):
    spool = tmp_path / "spool"
    _status(spool, "h-1", dropped_queue=5)
    health.check(spool, 1 << 30, NOW, timedelta(minutes=10))
    _status(spool, "h-1", dropped_queue=2, errors=1)
    [ev] = health.check(spool, 1 << 30, NOW, timedelta(minutes=10))
    assert ev["severity"] == "critical" and ev["attrs"]["dropped_queue"] == 2


def test_health_spool_level_and_stale(tmp_path: Path):
    spool = tmp_path / "spool"
    f = spool / "2026-10-03" / "07.h-1.jsonl"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 90)
    old = NOW.timestamp() - 3 * 3600
    os.utime(f, (old, old))
    kinds = {e["kind"] for e in health.check(spool, 100, NOW, timedelta(minutes=10))}
    assert kinds == {"spool_high", "ship_stale"}
    assert health.check(spool, 100, NOW, timedelta(minutes=10)) == []


def test_dispatch_filters_by_severity_and_survives_failing_notifier():
    got = []

    class Good:
        def notify(self, event):
            got.append(event["kind"])

    class Bad:
        def notify(self, event):
            raise RuntimeError("down")

    events = [health.event("a", "info", "x"), health.event("b", "warning", "y"), health.event("c", "critical", "z")]
    assert health.dispatch(events, [Bad(), Good()], "warning") == 2
    assert got == ["b", "c"]
