"""Exporters, importers and notifiers: the built-ins, and the opt-in loader for plugins.

Interfaces (structural; plugins do not import anything from this package):
    Exporter.export(records: Iterable[dict]) -> int      records are already redacted
    Exporter.close() -> None                              optional
    Importer.records() -> Iterator[dict]                  validated and redacted by the caller
    Notifier.notify(event: dict) -> None                  event = {kind, severity, summary, attrs}; no content

Plugins load only from the export, import and notify stages, and only when the profile sets
[plugins] allow = true. The proxy-side callback has no loader at all.
"""

from __future__ import annotations

import importlib
import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Literal, Protocol

from .profile import Component, Profile, ProfileError, resolve_env_options
from .records import open_records

Stage = Literal["export", "import", "notify"]


class Exporter(Protocol):
    def export(self, records: Iterable[dict[str, Any]]) -> int: ...


class Importer(Protocol):
    def records(self) -> Iterator[dict[str, Any]]: ...


class Notifier(Protocol):
    def notify(self, event: dict[str, Any]) -> None: ...


# ---------------------------------------------------------------- built-in exporters

class JsonlExporter:
    """Write records as JSON lines to a file, or to stdout with path = "-"."""

    def __init__(self, path: str) -> None:
        self.path = path

    def export(self, records: Iterable[dict[str, Any]]) -> int:
        n = 0
        if self.path == "-":
            for rec in records:
                sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
            return n
        with open(Path(self.path).expanduser(), "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        return n


def victorialogs_line(rec: dict[str, Any]) -> dict[str, Any]:
    """VictoriaLogs flattens nested objects, so content travels as JSON strings and scalars stay fields."""
    usage = rec.get("usage") or {}
    caller = rec.get("caller") or {}
    return {
        "_time": rec.get("ts_start"),
        "_msg": f"{rec.get('status')} {rec.get('model_group') or rec.get('model')} call {rec.get('call_id')}",
        "call_id": rec.get("call_id"), "id": rec.get("id"), "trace_id": rec.get("trace_id"),
        "status": rec.get("status"), "model": rec.get("model"), "model_group": rec.get("model_group"),
        "key_alias": caller.get("key_alias"), "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "messages_json": json.dumps(rec.get("messages"), ensure_ascii=False),
        "response_json": json.dumps(rec.get("response"), ensure_ascii=False),
        "error_json": json.dumps(rec.get("error"), ensure_ascii=False),
    }


class VictoriaLogsExporter:
    """POST to <url>/insert/jsonline (docs.victoriametrics.com/victorialogs/data-ingestion/)."""

    def __init__(self, url: str, batch: int = 200, stream_fields: str = "model_group,key_alias",
                 timeout_s: float = 30.0, opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        query = urllib.parse.urlencode({"_msg_field": "_msg", "_time_field": "_time", "_stream_fields": stream_fields})
        self.endpoint = url.rstrip("/") + "/insert/jsonline?" + query
        self.batch = batch
        self.timeout_s = timeout_s
        self._open = opener

    def _post(self, lines: list[str]) -> None:
        req = urllib.request.Request(self.endpoint, data=("\n".join(lines) + "\n").encode(),
                                     headers={"Content-Type": "application/stream+json"}, method="POST")
        with self._open(req, timeout=self.timeout_s) as resp:
            resp.read()

    def export(self, records: Iterable[dict[str, Any]]) -> int:
        n, buf = 0, []
        for rec in records:
            buf.append(json.dumps(victorialogs_line(rec), ensure_ascii=False))
            if len(buf) >= self.batch:
                self._post(buf)
                n += len(buf)
                buf = []
        if buf:
            self._post(buf)
            n += len(buf)
        return n


# ---------------------------------------------------------------- built-in importers

class JsonlImporter:
    """Read ledger records from a .jsonl or .jsonl.zst file (for example another ledger's export)."""

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()

    def records(self) -> Iterator[dict[str, Any]]:
        yield from open_records(self.path)


# ---------------------------------------------------------------- built-in notifiers

class WebhookNotifier:
    """POST each event as JSON to a URL (put secrets in url_env, not in the profile)."""

    def __init__(self, url: str, timeout_s: float = 5.0, opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._open = opener

    def notify(self, event: dict[str, Any]) -> None:
        req = urllib.request.Request(self.url, data=json.dumps(event).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self._open(req, timeout=self.timeout_s) as resp:
            resp.read()


class StderrNotifier:
    def notify(self, event: dict[str, Any]) -> None:
        print(f"[content-ledger] {event['severity']}: {event['kind']}: {event['summary']}", file=sys.stderr)


BUILTINS: dict[Stage, dict[str, Callable[..., Any]]] = {
    "export": {"jsonl": JsonlExporter, "victorialogs": VictoriaLogsExporter},
    "import": {"jsonl": JsonlImporter},
    "notify": {"webhook": WebhookNotifier, "stderr": StderrNotifier},
}
REQUIRED_METHOD: dict[Stage, str] = {"export": "export", "import": "records", "notify": "notify"}


# ---------------------------------------------------------------- loader

def _load_plugin(entry: Any, profile: Profile) -> Callable[..., Any]:
    if not profile.allow_plugins:
        raise ProfileError("type = 'plugin' needs [plugins] allow = true in the profile (plugins are off by default)")
    if not isinstance(entry, str) or ":" not in entry:
        raise ProfileError(f"plugin entry {entry!r} must look like 'module:attribute'")
    for p in profile.plugin_paths:
        expanded = str(Path(p).expanduser())
        if expanded not in sys.path:
            sys.path.insert(0, expanded)
    module_name, attr = entry.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ProfileError(f"plugin module {module_name!r} could not be imported: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ProfileError(f"plugin {entry!r}: {module_name} has no attribute {attr!r}") from exc


def build(stage: Stage, component: Component, profile: Profile) -> Any:
    options = resolve_env_options(dict(component.options))
    if component.type == "plugin":
        factory = _load_plugin(options.pop("entry", None), profile)
    else:
        factory = BUILTINS[stage].get(component.type)
        if factory is None:
            known = ", ".join(sorted(BUILTINS[stage])) + ", plugin"
            raise ProfileError(f"unknown {stage} type {component.type!r} (known: {known})")
    try:
        instance = factory(**options)
    except TypeError as exc:
        raise ProfileError(f"{stage} {component.type}: bad options: {exc}") from exc
    method = REQUIRED_METHOD[stage]
    if not callable(getattr(instance, method, None)):
        raise ProfileError(f"{stage} {component.type} has no {method}() method")
    return instance


def build_all(stage: Stage, profile: Profile) -> list[Any]:
    items = {"export": profile.exports, "import": profile.imports, "notify": profile.notifiers}[stage]
    return [build(stage, c, profile) for c in items]
