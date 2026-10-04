from pathlib import Path

from content_ledger import records as rec_mod
from content_ledger import search
from fixtures import rec, write_jsonl


def _roots(tmp_path: Path) -> list[Path]:
    spool, store = tmp_path / "spool", tmp_path / "store"
    write_jsonl(store / "2026/10/02/23.h-1.jsonl", [rec("c1", "2026-10-02T23:10:00+00:00", "sess-A", text="first")])
    write_jsonl(spool / "2026-10-03" / "08.h-1.jsonl", [
        rec("c3", "2026-10-03T08:30:00+00:00", "sess-A", text="third"),
        rec("c2", "2026-10-03T08:05:00+00:00", "sess-A", text="second"),
        rec("c4", "2026-10-03T08:40:00+00:00", "sess-B", model="local", text="unrelated MAGIC words"),
    ])
    return [store, spool]


def test_get_by_call_id_and_response_id(tmp_path: Path):
    roots = _roots(tmp_path)
    assert search.get(roots, "c3")["trace_id"] == "sess-A"
    assert search.get(roots, "resp-c1")["call_id"] == "c1"
    assert search.get(roots, "nope") is None


def test_find_filters(tmp_path: Path):
    roots = _roots(tmp_path)
    assert [r["call_id"] for r in search.find(roots, model="local")] == ["c4"]
    assert [r["call_id"] for r in search.find(roots, text="magic")] == ["c4"]
    assert sorted(r["call_id"] for r in search.find(roots, since="2026-10-03T00:00:00+00:00")) == ["c2", "c3", "c4"]
    assert search.find(roots, caller="nobody") == []
    assert len(search.find(roots, limit=2)) == 2


def test_trace_orders_by_time_across_roots(tmp_path: Path):
    assert [r["call_id"] for r in search.trace(_roots(tmp_path), "sess-A")] == ["c1", "c2", "c3"]


def test_trace_matches_session_header_when_trace_id_differs(tmp_path: Path):
    roots = _roots(tmp_path)
    write_jsonl(tmp_path / "spool" / "2026-10-03" / "09.h-1.jsonl",
                [rec("c5", "2026-10-03T09:00:00+00:00", "other", correlation={"headers": {"x-litellm-session-id": "sess-A"}})])
    assert [r["call_id"] for r in search.trace(roots, "sess-A")] == ["c1", "c2", "c3", "c5"]


def test_stats(tmp_path: Path):
    roots = _roots(tmp_path)
    s = search.stats(roots)
    assert (s["records"], s["files"]) == (4, 2)
    assert s["by_day"]["2026-10-03"]["records"] == 3


def test_corrupt_line_skipped(tmp_path: Path):
    roots = _roots(tmp_path)
    with open(tmp_path / "spool" / "2026-10-03" / "08.h-1.jsonl", "a") as fh:
        fh.write("{not json\n")
    assert len(search.find(roots)) == 4


def test_files_outside_window_not_opened(tmp_path: Path, monkeypatch):
    roots = _roots(tmp_path)
    opened: list[Path] = []
    original = rec_mod.open_records
    monkeypatch.setattr(rec_mod, "open_records", lambda p, zstd=None: (opened.append(p), original(p))[1])
    search.find(roots, since="2026-10-03T00:00:00+00:00")
    assert opened and all("2026/10/02" not in str(p) for p in opened)
