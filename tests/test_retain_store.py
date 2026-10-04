from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from content_ledger.retain import apply, plan_deletions
from content_ledger.store import DayUsage, LocalStore, parse_du_line, validate_day_path

TODAY = date(2026, 10, 3)
GIB = 1 << 30


def _day(offset: int, size: int) -> DayUsage:
    d = TODAY - timedelta(days=offset)
    return DayUsage(day=d, rel_path=d.strftime("%Y/%m/%d"), bytes=size)


def test_age_rule_only():
    plan = plan_deletions([_day(200, 1), _day(181, 1), _day(180, 1), _day(0, 1)], TODAY, 180, 10 * GIB)
    assert [(p.usage.day, p.reason) for p in plan] == [(TODAY - timedelta(days=200), "age"),
                                                       (TODAY - timedelta(days=181), "age")]


def test_size_rule_oldest_first_until_under_cap():
    plan = plan_deletions([_day(3, 4 * GIB), _day(2, 4 * GIB), _day(1, 4 * GIB), _day(0, GIB)], TODAY, 180, 10 * GIB)
    assert [(p.usage.day, p.reason) for p in plan] == [(TODAY - timedelta(days=3), "size")]


def test_age_then_size():
    plan = plan_deletions([_day(190, GIB), _day(5, 6 * GIB), _day(4, 6 * GIB), _day(0, 1)], TODAY, 180, 10 * GIB)
    assert [(p.usage.day, p.reason) for p in plan] == [(TODAY - timedelta(days=190), "age"),
                                                       (TODAY - timedelta(days=5), "size")]


def test_today_never_deleted():
    assert plan_deletions([_day(0, 20 * GIB)], TODAY, 180, 10 * GIB) == []


@pytest.mark.parametrize(("line", "expected"), [
    ("12\t2026/10/02", DayUsage(date(2026, 10, 2), "2026/10/02", 12 * 1024)),
    ("4\t_ops", None), ("4\t2026/13/40", None), ("garbage", None)])
def test_parse_du_line(line, expected):
    assert parse_du_line(line) == expected


@pytest.mark.parametrize("bad", ["2026/10/02/..", "../2026/10/02", "2026/10/02;rm", "", "/"])
def test_day_path_validation(bad):
    with pytest.raises(ValueError):
        validate_day_path(bad)


def test_local_store_usage_delete_and_log(tmp_path: Path):
    store = LocalStore(str(tmp_path))
    for rel in ("2026/04/01", "2026/10/03"):
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "08.h-1.jsonl.zst").write_bytes(b"x" * 10)
    (tmp_path / "_ops").mkdir()
    usage = store.usage()
    assert [u.rel_path for u in usage] == ["2026/04/01", "2026/10/03"]
    lines = apply(store, plan_deletions(usage, TODAY, 180, 10 * GIB), datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert len(lines) == 1 and "deleted 2026/04/01" in lines[0]
    assert not (tmp_path / "2026/04").exists()
    assert (tmp_path / "2026/10/03").is_dir()
    assert "deleted 2026/04/01" in (tmp_path / "_ops" / "retention.log").read_text()


def test_local_store_upload_is_private(tmp_path: Path):
    src = tmp_path / "f"
    src.write_bytes(b"data")
    store = LocalStore(str(tmp_path / "store"))
    store.upload(src, "2026/10/03/08.h-1.jsonl.zst")
    assert (tmp_path / "store/2026/10/03/08.h-1.jsonl.zst").stat().st_mode & 0o777 == 0o600
    assert store.sha256("2026/10/03/08.h-1.jsonl.zst") == store.sha256("2026/10/03/08.h-1.jsonl.zst")
    assert store.sha256("missing") is None
