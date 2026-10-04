import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import proxy_callback_import as cl
from fixtures import LIVE_KEY, make_kwargs, make_slp


def _ledger(tmp_path: Path, **kw) -> cl.ContentLedger:
    writer = cl.LedgerWriter(spool=tmp_path / "spool", spool_cap_bytes=10**9, queue_budget_bytes=10**8,
                             clock=lambda: datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc))
    return cl.ContentLedger(writer=writer, **kw)


def _records(tmp_path: Path) -> list[dict]:
    return [json.loads(x) for f in (tmp_path / "spool").rglob("*.jsonl") for x in f.read_text().splitlines()]


def test_success_event_writes_one_record(tmp_path: Path):
    ledger = _ledger(tmp_path)
    asyncio.run(ledger.async_log_success_event(make_kwargs(), None, None, None))
    ledger.writer.stop()
    records = _records(tmp_path)
    assert len(records) == 1 and records[0]["status"] == "success"
    assert LIVE_KEY not in json.dumps(records)


def test_failure_event_writes_failure_record(tmp_path: Path):
    ledger = _ledger(tmp_path)
    asyncio.run(ledger.async_log_failure_event(make_kwargs(make_slp(response=None, error_str="boom")), None, None, None))
    ledger.writer.stop()
    assert _records(tmp_path)[0]["error"]["str"] == "boom"


def test_header_prefixes_from_constructor(tmp_path: Path):
    ledger = _ledger(tmp_path, header_prefixes=("x-agent-",))
    asyncio.run(ledger.async_log_success_event(make_kwargs(), None, None, None))
    ledger.writer.stop()
    assert _records(tmp_path)[0]["correlation"]["headers"]["x-agent-task"] == "task-7"


def test_bad_kwargs_never_raise(tmp_path: Path):
    ledger = _ledger(tmp_path)
    asyncio.run(ledger.async_log_success_event({"standard_logging_object": "not-a-dict"}, None, None, None))
    asyncio.run(ledger.async_log_success_event(None, None, None, None))
    ledger.writer.stop()
    assert ledger.writer.stats["build_errors"] == 2
    assert _records(tmp_path) == []


def test_unserializable_value_is_stringified(tmp_path: Path):
    ledger = _ledger(tmp_path)
    asyncio.run(ledger.async_log_success_event(make_kwargs(make_slp(response={"obj": object()})), None, None, None))
    ledger.writer.stop()
    assert _records(tmp_path)[0]["response"]["obj"].startswith("<object object")


def test_handler_cost_on_loop_for_large_record(tmp_path: Path):
    ledger = _ledger(tmp_path)
    kwargs = make_kwargs(make_slp(messages=[{"role": "user", "content": "word " * 70_000}]))
    ledger.writer.pause()
    runs = []
    for _ in range(20):
        t = time.perf_counter()
        asyncio.run(ledger.async_log_success_event(kwargs, None, None, None))
        runs.append((time.perf_counter() - t) * 1000)
    ledger.writer.resume()
    ledger.writer.stop(10)
    runs.sort()
    print(f"handler ms for 350 KB record: p50={runs[10]:.3f} max={runs[-1]:.3f}")
    assert runs[10] < 25
