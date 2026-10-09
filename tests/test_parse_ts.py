from datetime import datetime, timedelta, timezone

import pytest

from content_ledger.records import parse_ts


@pytest.mark.parametrize("value", [None, 0, False, b"2026-10-03T08:20:15", [], {}])
def test_parse_ts_rejects_non_string_values(value):
    assert parse_ts(value) is None


@pytest.mark.parametrize("value", ["", "not-a-timestamp", "2026-02-29T00:00:00", "2026-10-03T08:20:15 trailing"])
def test_parse_ts_rejects_empty_and_malformed_strings(value):
    assert parse_ts(value) is None


def test_parse_ts_assigns_utc_to_naive_datetime_boundaries():
    assert parse_ts("0001-01-01T00:00:00") == datetime(1, 1, 1, tzinfo=timezone.utc)
    assert parse_ts("9999-12-31T23:59:59.999999") == datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)


def test_parse_ts_preserves_explicit_timezone_and_accepts_iso_date():
    assert parse_ts("2026-10-03T08:20:15+05:30") == datetime(
        2026, 10, 3, 8, 20, 15, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    assert parse_ts("2026-10-03") == datetime(2026, 10, 3, tzinfo=timezone.utc)
