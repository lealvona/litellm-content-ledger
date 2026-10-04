"""Profiles: every instance-specific value in one TOML or JSON file.

Lookup: --profile PATH, else $CONTENT_LEDGER_PROFILE, else ~/.config/content-ledger/profile.toml.
Values ending in `_env` name an environment variable instead of holding a secret inline:
`url_env = "ALERT_URL"` is read as `url = os.environ["ALERT_URL"]` when the component is built.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = "~/.config/content-ledger/profile.toml"
SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)?\s*$")
UNITS = {None: 1, "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
         "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}
SEVERITIES = ("info", "warning", "critical")


class ProfileError(ValueError):
    pass


def parse_size(value: Any, what: str) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    m = SIZE.match(str(value))
    if not m:
        raise ProfileError(f"{what}: {value!r} is not a size like 10GiB or 500MB")
    return int(float(m.group(1)) * UNITS[m.group(2)])


@dataclass(frozen=True, slots=True)
class Component:
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Profile:
    spool: str
    spool_cap_bytes: int
    store_type: str
    store_path: str
    store_host: str | None
    read_root: str | None
    max_age_days: int
    max_bytes: int
    zstd: str | None
    zstd_level: int
    ship_grace_minutes: int
    builtin_redaction: bool
    redaction_rules: tuple[dict[str, str], ...]
    exports: tuple[Component, ...]
    imports: tuple[Component, ...]
    notifiers: tuple[Component, ...]
    notify_min_severity: str
    allow_plugins: bool
    plugin_paths: tuple[str, ...]
    source: str = ""

    def spool_path(self) -> Path:
        return Path(self.spool).expanduser()

    def search_roots(self) -> list[Path]:
        roots = [self.spool_path()]
        if self.store_type == "local":
            roots.append(Path(self.store_path).expanduser())
        elif self.read_root:
            roots.append(Path(self.read_root).expanduser())
        return roots

    def to_dict(self) -> dict[str, Any]:
        """Normalized, portable form (what `profile export` writes). Inline secrets are never present because
        secrets are referenced through *_env keys."""
        d = asdict(self)
        d.pop("source")
        return d


def _components(raw: Any, section: str) -> tuple[Component, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProfileError(f"[[{section}]] must be an array of tables")
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
            raise ProfileError(f"[[{section}]] entry {i} needs a string 'type'")
        opts = {k: v for k, v in entry.items() if k != "type"}
        out.append(Component(entry["type"], opts))
    return tuple(out)


def from_dict(raw: dict[str, Any], source: str = "") -> Profile:
    ledger = raw.get("ledger") or {}
    store = raw.get("store") or {}
    retention = raw.get("retention") or {}
    compress = raw.get("compress") or {}
    redaction = raw.get("redaction") or {}
    plugins = raw.get("plugins") or {}
    notify_cfg = raw.get("notify_settings") or {}

    store_type = store.get("type")
    if store_type not in ("local", "ssh"):
        raise ProfileError("[store] type must be 'local' or 'ssh'")
    store_path = store.get("path")
    if not isinstance(store_path, str) or not store_path:
        raise ProfileError("[store] path is required")
    host = store.get("host")
    if store_type == "ssh" and not host:
        raise ProfileError("[store] host is required when type = 'ssh' (e.g. user@nas)")

    max_age = retention.get("max_age_days")
    if not isinstance(max_age, int) or max_age < 1:
        raise ProfileError("[retention] max_age_days must be a positive integer")
    if "max_bytes" not in retention:
        raise ProfileError("[retention] max_bytes is required (e.g. \"10GiB\")")

    severity = notify_cfg.get("min_severity", "warning")
    if severity not in SEVERITIES:
        raise ProfileError(f"[notify_settings] min_severity must be one of {SEVERITIES}")

    rules = redaction.get("rules") or []
    if not isinstance(rules, list) or not all(isinstance(r, dict) and "name" in r and "pattern" in r for r in rules):
        raise ProfileError("[[redaction.rules]] entries need 'name' and 'pattern'")

    return Profile(
        spool=str(ledger.get("spool", "~/.local/state/content-ledger/spool")),
        spool_cap_bytes=parse_size(ledger.get("spool_cap", "1GiB"), "[ledger] spool_cap"),
        store_type=store_type,
        store_path=store_path,
        store_host=host,
        read_root=store.get("read_root"),
        max_age_days=max_age,
        max_bytes=parse_size(retention["max_bytes"], "[retention] max_bytes"),
        zstd=compress.get("zstd"),
        zstd_level=int(compress.get("level", 19)),
        ship_grace_minutes=int(ledger.get("ship_grace_minutes", 10)),
        builtin_redaction=bool(redaction.get("builtin", True)),
        redaction_rules=tuple({"name": str(r["name"]), "pattern": str(r["pattern"])} for r in rules),
        exports=_components(raw.get("export"), "export"),
        imports=_components(raw.get("import"), "import"),
        notifiers=_components(raw.get("notify"), "notify"),
        notify_min_severity=severity,
        allow_plugins=bool(plugins.get("allow", False)),
        plugin_paths=tuple(str(p) for p in plugins.get("paths", [])),
        source=source,
    )


def to_raw(profile: Profile) -> dict[str, Any]:
    """Inverse of from_dict, for `profile export`: a file `profile import` (and from_dict) accepts as-is."""
    def comps(items: tuple[Component, ...]) -> list[dict[str, Any]]:
        return [{"type": c.type, **c.options} for c in items]

    raw: dict[str, Any] = {
        "ledger": {"spool": profile.spool, "spool_cap": profile.spool_cap_bytes,
                   "ship_grace_minutes": profile.ship_grace_minutes},
        "store": {k: v for k, v in {"type": profile.store_type, "path": profile.store_path,
                                     "host": profile.store_host, "read_root": profile.read_root}.items() if v},
        "retention": {"max_age_days": profile.max_age_days, "max_bytes": profile.max_bytes},
        "compress": {k: v for k, v in {"zstd": profile.zstd, "level": profile.zstd_level}.items() if v is not None},
        "redaction": {"builtin": profile.builtin_redaction, "rules": list(profile.redaction_rules)},
        "notify_settings": {"min_severity": profile.notify_min_severity},
        "plugins": {"allow": profile.allow_plugins, "paths": list(profile.plugin_paths)},
        "export": comps(profile.exports),
        "import": comps(profile.imports),
        "notify": comps(profile.notifiers),
    }
    return raw


def read_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    return tomllib.loads(text)


def resolve_path(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get("CONTENT_LEDGER_PROFILE") or DEFAULT_PATH).expanduser()


def load(explicit: str | None = None) -> Profile:
    path = resolve_path(explicit)
    if not path.exists():
        raise ProfileError(f"no profile at {path}; copy examples/profile.toml there or pass --profile")
    try:
        return from_dict(read_file(path), source=str(path))
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc


def resolve_env_options(options: dict[str, Any]) -> dict[str, Any]:
    """Turn `x_env = "VAR"` into `x = $VAR`; a missing variable is an error, never a silent empty value."""
    out = {}
    for key, value in options.items():
        if key.endswith("_env") and isinstance(value, str):
            name = key[: -len("_env")]
            if value not in os.environ:
                raise ProfileError(f"option {key} names ${value}, which is not set")
            out[name] = os.environ[value]
        else:
            out[key] = value
    return out
