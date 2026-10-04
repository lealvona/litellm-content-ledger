import hashlib
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from content_ledger.redact import Redactor
from content_ledger.ship import closed_files, ship, zstd_compressor
from content_ledger.store import LocalStore

NOW = datetime(2026, 10, 3, 10, 15, tzinfo=timezone.utc)
GRACE = timedelta(minutes=10)


class CorruptingStore(LocalStore):
    def upload(self, local: Path, rel_path: str) -> None:
        super().upload(local, rel_path)
        dest = self.root / rel_path
        dest.write_bytes(dest.read_bytes() + b"x")


class CountingStore(LocalStore):
    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.uploads: list[str] = []

    def upload(self, local: Path, rel_path: str) -> None:
        super().upload(local, rel_path)
        self.uploads.append(rel_path)


def fake_compress(src: Path, out: Path) -> None:
    out.write_bytes(b"Z" + src.read_bytes())


def _spool_file(spool: Path, day: str, hour: str, body: bytes = b'{"n": 1}\n', age_s: int = 3600) -> Path:
    d = spool / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{hour}.host-1.jsonl"
    f.write_bytes(body)
    t = NOW.timestamp() - age_s
    os.utime(f, (t, t))
    return f


def _ship(spool: Path, store, **kw):
    return ship(spool, store, NOW, fake_compress, Redactor(), grace=GRACE, **kw)


def test_closed_files_skip_current_hour(tmp_path: Path):
    spool = tmp_path / "spool"
    h08 = _spool_file(spool, "2026-10-03", "08")
    h09 = _spool_file(spool, "2026-10-03", "09", age_s=60 * 14)
    _spool_file(spool, "2026-10-03", "10", age_s=600)
    assert closed_files(spool, NOW, GRACE) == [h08, h09]


def test_closed_files_respect_grace_and_idle(tmp_path: Path):
    spool = tmp_path / "spool"
    _spool_file(spool, "2026-10-03", "10", age_s=3600)
    assert closed_files(spool, datetime(2026, 10, 3, 11, 5, tzinfo=timezone.utc), GRACE) == []
    _spool_file(spool, "2026-10-03", "07", age_s=30)
    assert closed_files(spool, NOW, GRACE) == []


def test_closed_files_ignore_status_and_state(tmp_path: Path):
    spool = tmp_path / "spool"
    (spool / "_status").mkdir(parents=True)
    (spool / "_status" / "h-1.json").write_text("{}")
    assert closed_files(spool, NOW, GRACE) == []


def test_ship_deletes_spool_only_after_hash_match(tmp_path: Path):
    spool = tmp_path / "spool"
    f = _spool_file(spool, "2026-10-02", "23")
    store = CountingStore(str(tmp_path / "store"))
    report = _ship(spool, store)
    assert (report.shipped, report.failed) == (1, 0)
    assert store.uploads == ["2026/10/02/23.host-1.jsonl.zst"]
    assert (tmp_path / "store" / "2026/10/02/23.host-1.jsonl.zst").read_bytes() == b'Z{"n": 1}\n'
    assert not f.exists() and not (spool / "2026-10-02").exists()


def test_ship_redacts_before_upload_and_leaves_no_temp_files(tmp_path: Path):
    spool = tmp_path / "spool"
    _spool_file(spool, "2026-10-02", "23", body=b'{"m": "Authorization: Bearer abcdefghijklmnop"}\n')
    report = _ship(spool, LocalStore(str(tmp_path / "store")))
    shipped = (tmp_path / "store" / "2026/10/02/23.host-1.jsonl.zst").read_bytes()
    assert b"abcdefghijklmnop" not in shipped and b"[REDACTED:bearer]" in shipped
    assert report.redactions == 1
    assert [p for p in spool.rglob("*") if p.is_file()] == []


def test_torn_line_is_counted_not_fatal(tmp_path: Path):
    spool = tmp_path / "spool"
    _spool_file(spool, "2026-10-02", "23", body=b'{"n": 1}\n{"n": 2, "trunc')
    report = _ship(spool, LocalStore(str(tmp_path / "store")))
    assert (report.shipped, report.unparseable_lines) == (1, 1)


def test_ship_keeps_everything_on_hash_mismatch(tmp_path: Path):
    spool = tmp_path / "spool"
    f = _spool_file(spool, "2026-10-02", "23")
    report = _ship(spool, CorruptingStore(str(tmp_path / "store")))
    assert (report.shipped, report.failed) == (0, 1)
    assert f.exists()


def test_ship_never_overwrites_a_different_store_file(tmp_path: Path):
    spool = tmp_path / "spool"
    _spool_file(spool, "2026-10-02", "23", body=b'{"n": 2}\n')
    existing = tmp_path / "store" / "2026/10/02/23.host-1.jsonl.zst"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"older content")
    store = CountingStore(str(tmp_path / "store"))
    assert _ship(spool, store).shipped == 1
    assert existing.read_bytes() == b"older content"
    assert len(store.uploads) == 1 and store.uploads[0] != "2026/10/02/23.host-1.jsonl.zst"


def test_ship_skips_upload_when_identical_file_present(tmp_path: Path):
    spool = tmp_path / "spool"
    f = _spool_file(spool, "2026-10-02", "23")
    existing = tmp_path / "store" / "2026/10/02/23.host-1.jsonl.zst"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b'Z{"n": 1}\n')
    store = CountingStore(str(tmp_path / "store"))
    assert _ship(spool, store).shipped == 1 and store.uploads == [] and not f.exists()


def test_ship_keeps_today_dir(tmp_path: Path):
    spool = tmp_path / "spool"
    _spool_file(spool, "2026-10-03", "08")
    _ship(spool, LocalStore(str(tmp_path / "store")))
    assert (spool / "2026-10-03").is_dir()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")
def test_real_zstd_round_trip(tmp_path: Path):
    src = tmp_path / "08.h-1.jsonl"
    src.write_bytes(b'{"a": 1}\n' * 1000)
    out = tmp_path / "08.h-1.jsonl.zst"
    zstd_compressor(shutil.which("zstd"), 19)(src, out)
    assert 0 < out.stat().st_size < src.stat().st_size
    assert hashlib.sha256(src.read_bytes()).hexdigest()
