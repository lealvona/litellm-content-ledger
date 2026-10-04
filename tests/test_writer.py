import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import proxy_callback_import as cl

T0 = datetime(2026, 10, 3, 8, 59, 59, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _writer(tmp_path: Path, clock: Clock, *, cap: int = 10**9, budget: int = 10**8, wake: float = 1.0, root=None):
    return cl.LedgerWriter(spool=tmp_path / "spool", spool_cap_bytes=cap, queue_budget_bytes=budget, clock=clock,
                           root_device=root, wake_interval_s=wake)


def _lines(spool: Path) -> list[dict]:
    return [json.loads(x) for f in sorted(spool.rglob("*.jsonl")) for x in f.read_text().splitlines()]


def test_writes_line_verbatim_into_private_hour_file(tmp_path: Path):
    w = _writer(tmp_path, Clock(T0))
    assert w.submit(json.dumps({"k": "Bearer abcdefghijklmnop"}).encode())
    w.flush(5)
    w.stop()
    files = list((tmp_path / "spool" / "2026-10-03").glob("08.*.jsonl"))
    assert [f.name for f in files] == [f"08.{os.uname().nodename}-{os.getpid()}.jsonl"]
    assert _lines(tmp_path / "spool") == [{"k": "Bearer abcdefghijklmnop"}]
    assert (tmp_path / "spool").stat().st_mode & 0o777 == 0o700
    assert w.stats["written"] == 1


def test_submit_does_not_wake_writer_until_interval_or_flush(tmp_path: Path):
    w = _writer(tmp_path, Clock(T0), wake=60)
    time.sleep(0.1)
    w.submit(b'{"n": 1}')
    w.submit(b'{"n": 2}')
    time.sleep(0.3)
    assert w.stats["written"] == 0
    assert w.flush(5)
    assert w.stats["written"] == 2
    w.stop()


def test_hour_rollover_opens_new_file(tmp_path: Path):
    clock = Clock(T0)
    w = _writer(tmp_path, clock)
    w.submit(b'{"n": 1}')
    w.flush(5)
    clock.now = T0 + timedelta(seconds=2)
    w.submit(b'{"n": 2}')
    w.flush(5)
    w.stop()
    names = sorted(p.name.split(".")[0] for p in (tmp_path / "spool" / "2026-10-03").glob("*.jsonl"))
    assert names == ["08", "09"]


def test_queue_budget_drops_and_counts(tmp_path: Path):
    w = _writer(tmp_path, Clock(T0), budget=100)
    w.pause()
    assert w.submit(b"x" * 60)
    assert not w.submit(b"y" * 60)
    w.resume()
    w.flush(5)
    w.stop()
    assert (w.stats["dropped_queue"], w.stats["written"]) == (1, 1)


def test_spool_cap_drops_and_counts(tmp_path: Path):
    w = _writer(tmp_path, Clock(T0), cap=50)
    w.submit(b'{"a": "' + b"z" * 40 + b'"}')
    w.submit(b'{"b": "' + b"z" * 40 + b'"}')
    w.flush(5)
    w.stop()
    assert (w.stats["written"], w.stats["dropped_spool"]) == (1, 1)


def test_existing_spool_counts_toward_cap(tmp_path: Path):
    old = tmp_path / "spool" / "2026-10-02"
    old.mkdir(parents=True)
    (old / "23.host-1.jsonl").write_bytes(b"x" * 100)
    w = _writer(tmp_path, Clock(T0), cap=120)
    w.submit(b'{"a": "' + b"z" * 40 + b'"}')
    w.flush(5)
    w.stop()
    assert w.stats["dropped_spool"] == 1


def test_write_error_is_counted_and_thread_survives(tmp_path: Path):
    blocker = tmp_path / "spool"
    blocker.write_text("not a directory")
    w = _writer(tmp_path, Clock(T0))
    w.submit(b'{"n": 1}')
    w.flush(5)
    assert w.stats["errors"] == 1
    blocker.unlink()
    w.submit(b'{"n": 2}')
    w.flush(5)
    w.stop()
    assert w.stats["written"] == 1


def test_refuses_spool_on_root_device(tmp_path: Path):
    w = _writer(tmp_path, Clock(T0), root=os.stat(tmp_path).st_dev)
    w.submit(b'{"n": 1}')
    w.flush(5)
    w.stop()
    assert (w.stats["written"], w.stats["errors"]) == (0, 1)
    assert list((tmp_path / "spool").rglob("*.jsonl")) == []


def test_status_file_reports_counters(tmp_path: Path):
    w = _writer(tmp_path, Clock(T0), budget=100)
    w.pause()
    w.submit(b"x" * 60)
    w.submit(b"y" * 60)
    w.resume()
    w.flush(5)
    w.stop()
    status_files = list((tmp_path / "spool" / "_status").glob("*.json"))
    assert len(status_files) == 1
    status = json.loads(status_files[0].read_text())
    assert (status["written"], status["dropped_queue"]) == (1, 1)
    assert status["tag"] == f"{os.uname().nodename}-{os.getpid()}"


def test_submit_after_stop_is_dropped_not_raised(tmp_path: Path):
    w = _writer(tmp_path, Clock(T0))
    w.stop()
    assert not w.submit(b'{"n": 1}')
