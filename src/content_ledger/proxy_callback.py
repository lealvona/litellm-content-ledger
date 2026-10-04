"""LiteLLM proxy callback: record the full request and response of every call, off the request path.

This file is self-contained on purpose. LiteLLM loads `callbacks:` entries as single files from the config
file's directory, so install it there as `content_ledger.py` (`content-ledger install-callback <dir>`) and
reference `content_ledger.proxy_handler_instance`.

Constraints that make it safe inside the proxy (each one measured or tested, see docs/):
- The hook builds a record field by field from `standard_logging_object`, serializes it and enqueues it.
  `kwargs["api_key"]` holds the raw upstream provider key and is never read. LiteLLM awaits FAILURE callbacks
  inline before replying, so this must stay trivial.
- A writer thread wakes on a timer (not per record) and appends lines to an hourly spool file. Waking it per
  record made it compete for the GIL with the event loop.
- No regex, no compression, no network here. Secret redaction (GIL-bound regex work) runs in the separate
  ship process.
- Queue over its byte budget, spool over its cap, or spool on the root filesystem: drop and count, never
  block. Every exception is caught.
- Counters go to spool/_status/<host>-<pid>.json for `content-ledger health` to read.

Environment (all optional):
  CONTENT_LEDGER_SPOOL                     spool directory (default ~/.local/state/content-ledger/spool)
  CONTENT_LEDGER_SPOOL_CAP_BYTES           default 1 GiB
  CONTENT_LEDGER_QUEUE_BUDGET_BYTES        default 64 MiB
  CONTENT_LEDGER_WAKE_INTERVAL_S           default 1.0
  CONTENT_LEDGER_ALLOW_ROOT_FS             "1" to allow a spool on the root filesystem (default refuses)
  CONTENT_LEDGER_CORRELATION_HEADERS       extra request headers to keep, comma-separated
  CONTENT_LEDGER_CORRELATION_PREFIXES      header prefixes to keep, comma-separated (e.g. "x-myagent-")
"""

from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from litellm.integrations.custom_logger import CustomLogger

try:
    import orjson
except ImportError:  # orjson ships with LiteLLM; the stdlib path keeps the module loadable without it
    orjson = None

RECORD_VERSION = 1
DEFAULT_SPOOL = "~/.local/state/content-ledger/spool"
DEFAULT_SPOOL_CAP_BYTES = 1 << 30
DEFAULT_QUEUE_BUDGET_BYTES = 64 << 20
STATUS_MIN_INTERVAL_S = 60.0
BASE_CORRELATION_HEADERS = ("x-litellm-session-id", "x-litellm-trace-id")
COUNTERS = ("written", "dropped_queue", "dropped_spool", "errors", "build_errors", "skipped")

Status = Literal["success", "failure"]


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in os.environ.get(name, "").split(",") if p.strip())


# ---------------------------------------------------------------- record

def _iso(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return aware.astimezone(dt.timezone.utc).isoformat()
    return None


def correlation_headers(
    headers: Any, names: tuple[str, ...] = BASE_CORRELATION_HEADERS, prefixes: tuple[str, ...] = ()
) -> dict[str, Any]:
    if not isinstance(headers, Mapping):
        return {}
    return {
        k: v
        for k, v in headers.items()
        if isinstance(k, str) and (k.lower() in names or any(k.lower().startswith(p) for p in prefixes))
    }


def build_record(
    kwargs: Mapping[str, Any],
    status: Status,
    header_names: tuple[str, ...] = BASE_CORRELATION_HEADERS,
    header_prefixes: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """One ledger record from LiteLLM callback kwargs, or None when LiteLLM supplied no logging payload."""
    slp = kwargs.get("standard_logging_object")
    if slp is None:
        return None
    if not isinstance(slp, Mapping):
        raise TypeError(f"standard_logging_object is {type(slp).__name__}")
    md = slp.get("metadata") or {}
    params = kwargs.get("litellm_params") or {}
    return {
        "v": RECORD_VERSION,
        "ts_start": _iso(slp.get("startTime")),
        "ts_end": _iso(slp.get("endTime")),
        "status": status,
        "call_id": slp.get("litellm_call_id"),
        "id": slp.get("id"),
        "trace_id": slp.get("trace_id"),
        "call_type": slp.get("call_type"),
        "stream": slp.get("stream"),
        "cache_hit": slp.get("cache_hit"),
        "model": slp.get("model"),
        "model_group": slp.get("model_group"),
        "model_id": slp.get("model_id"),
        "provider": slp.get("custom_llm_provider"),
        "api_base": slp.get("api_base"),
        "caller": {
            "key_alias": md.get("user_api_key_alias"),
            "key_hash": md.get("user_api_key_hash"),
            "user_id": md.get("user_api_key_user_id"),
            "team_id": md.get("user_api_key_team_id"),
            "end_user": slp.get("end_user") or md.get("user_api_key_end_user_id"),
            "ip": slp.get("requester_ip_address"),
            "user_agent": slp.get("user_agent"),
        },
        "usage": {
            "prompt_tokens": slp.get("prompt_tokens"),
            "completion_tokens": slp.get("completion_tokens"),
            "total_tokens": slp.get("total_tokens"),
            "cost": slp.get("response_cost"),
        },
        "correlation": {
            "headers": correlation_headers(md.get("requester_custom_headers"), header_names, header_prefixes),
            "requester_metadata": md.get("requester_metadata"),
            "request_tags": slp.get("request_tags"),
        },
        "client_requested_no_log": params.get("no-log") is True,
        "model_parameters": slp.get("model_parameters"),
        "messages": slp.get("messages"),
        "response": slp.get("response"),
        "error": None
        if status == "success"
        else {"str": slp.get("error_str"), "information": slp.get("error_information")},
    }


def serialize(record: Mapping[str, Any]) -> bytes:
    if orjson is not None:
        return orjson.dumps(record, default=str, option=orjson.OPT_NON_STR_KEYS)
    return json.dumps(record, default=str, ensure_ascii=False, separators=(",", ":")).encode()


# ---------------------------------------------------------------- writer

def _dir_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


class LedgerWriter:
    """Bounded queue + one daemon thread appending JSON lines to spool/YYYY-MM-DD/HH.<host>-<pid>.jsonl.

    submit() never wakes the writer; it drains on its own every ``wake_interval_s``. Waking it per record
    made it compete for the GIL with the event loop while LiteLLM built error responses (+13-19 ms measured).
    """

    def __init__(
        self,
        spool: Path,
        spool_cap_bytes: int,
        queue_budget_bytes: int,
        clock: Callable[[], dt.datetime],
        root_device: int | None = None,
        wake_interval_s: float = 1.0,
    ) -> None:
        self.spool = spool
        self.spool_cap_bytes = spool_cap_bytes
        self.queue_budget_bytes = queue_budget_bytes
        self._clock = clock
        self._root_device = root_device
        self._wake_interval_s = wake_interval_s
        self._tag = f"{os.uname().nodename}-{os.getpid()}"
        self._items: deque[bytes] = deque()
        self._pending_bytes = 0
        self._busy = False
        self._stopped = False
        self._flush_requested = False
        self._paused = False
        self._cond = threading.Condition()
        self._stats = {k: 0 for k in COUNTERS}
        self._status_written: dict[str, int] | None = None
        self._status_at = 0.0
        self._spool_bytes = _dir_bytes(spool)
        self._thread = threading.Thread(target=self._run, name="content-ledger-writer", daemon=True)
        self._thread.start()

    @classmethod
    def from_env(cls) -> LedgerWriter:
        allow_root = os.environ.get("CONTENT_LEDGER_ALLOW_ROOT_FS") == "1"
        return cls(
            spool=Path(os.environ.get("CONTENT_LEDGER_SPOOL", DEFAULT_SPOOL)).expanduser(),
            spool_cap_bytes=int(os.environ.get("CONTENT_LEDGER_SPOOL_CAP_BYTES", DEFAULT_SPOOL_CAP_BYTES)),
            queue_budget_bytes=int(os.environ.get("CONTENT_LEDGER_QUEUE_BUDGET_BYTES", DEFAULT_QUEUE_BUDGET_BYTES)),
            clock=lambda: dt.datetime.now(dt.timezone.utc),
            root_device=None if allow_root else os.stat("/").st_dev,
            wake_interval_s=float(os.environ.get("CONTENT_LEDGER_WAKE_INTERVAL_S", "1.0")),
        )

    @property
    def stats(self) -> dict[str, int]:
        with self._cond:
            return dict(self._stats)

    def count(self, key: str, n: int = 1) -> None:
        with self._cond:
            self._stats[key] += n

    def submit(self, line: bytes) -> bool:
        """Called on the event loop: O(1), never blocks on I/O, never raises."""
        with self._cond:
            if self._stopped or self._pending_bytes + len(line) > self.queue_budget_bytes:
                self._stats["dropped_queue"] += 1
                return False
            self._items.append(line)
            self._pending_bytes += len(line)
            return True

    def pause(self) -> None:
        with self._cond:
            self._paused = True

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            self._flush_requested = True
            self._cond.notify_all()
            while self._items or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def stop(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._thread.join(timeout)
        self._write_status(force=True)

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._stopped and not self._flush_requested:
                    self._cond.wait(self._wake_interval_s)
                while self._paused and not self._stopped:
                    self._cond.wait()
                if self._stopped and not self._items:
                    return
                batch = list(self._items)
                self._items.clear()
                self._pending_bytes = 0
                self._flush_requested = False
                self._busy = bool(batch)
            for line in batch:
                try:
                    self._write(line)
                except Exception:
                    self.count("errors")
            self._write_status(force=False)
            with self._cond:
                self._busy = False
                self._cond.notify_all()

    def _ensure_dir(self, path: Path) -> None:
        if not self.spool.is_dir():
            self.spool.mkdir(mode=0o700, parents=True)
        path.mkdir(mode=0o700, exist_ok=True)

    def _write(self, raw: bytes) -> None:
        data = raw + b"\n"
        if self._spool_bytes + len(data) > self.spool_cap_bytes:
            self._spool_bytes = _dir_bytes(self.spool)
            if self._spool_bytes + len(data) > self.spool_cap_bytes:
                self.count("dropped_spool")
                return
        now = self._clock().astimezone(dt.timezone.utc)
        day_dir = self.spool / now.strftime("%Y-%m-%d")
        self._ensure_dir(day_dir)
        if self._root_device is not None and os.stat(day_dir).st_dev == self._root_device:
            self.count("dropped_spool")
            raise RuntimeError("spool is on the root filesystem; refusing to write")
        with open(day_dir / f"{now:%H}.{self._tag}.jsonl", "ab") as fh:
            fh.write(data)
        self._spool_bytes += len(data)
        self.count("written")

    def _write_status(self, force: bool) -> None:
        stats = self.stats
        now = time.monotonic()
        if not force:
            if stats == self._status_written:
                return
            if self._status_written is not None and now - self._status_at < STATUS_MIN_INTERVAL_S:
                return
        try:
            status_dir = self.spool / "_status"
            self._ensure_dir(status_dir)
            body = {"tag": self._tag, "updated": self._clock().astimezone(dt.timezone.utc).isoformat(),
                    "spool_cap_bytes": self.spool_cap_bytes, **stats}
            tmp = status_dir / f".{self._tag}.json.tmp"
            tmp.write_text(json.dumps(body))
            os.replace(tmp, status_dir / f"{self._tag}.json")
            self._status_written = stats
            self._status_at = now
        except Exception:
            pass


# ---------------------------------------------------------------- LiteLLM adapter

class ContentLedger(CustomLogger):
    def __init__(
        self,
        writer: LedgerWriter | None = None,
        header_names: tuple[str, ...] | None = None,
        header_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self._writer = writer
        self._writer_lock = threading.Lock()
        self._header_names = header_names if header_names is not None else (
            BASE_CORRELATION_HEADERS + _csv_env("CONTENT_LEDGER_CORRELATION_HEADERS"))
        self._header_prefixes = header_prefixes if header_prefixes is not None else _csv_env(
            "CONTENT_LEDGER_CORRELATION_PREFIXES")

    @property
    def writer(self) -> LedgerWriter:
        if self._writer is None:
            with self._writer_lock:
                if self._writer is None:
                    self._writer = LedgerWriter.from_env()
                    atexit.register(self._writer.stop, 2.0)
        return self._writer

    def _capture(self, kwargs: Any, status: Status) -> None:
        try:
            record = build_record(kwargs, status, self._header_names, self._header_prefixes)
        except Exception:
            self.writer.count("build_errors")
            return
        if record is None:
            self.writer.count("skipped")
            return
        try:
            self.writer.submit(serialize(record))
        except Exception:
            self.writer.count("build_errors")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        self._capture(kwargs, "success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        self._capture(kwargs, "failure")


proxy_handler_instance = ContentLedger()
