"""content-ledger: ship, retain, health, export, import, profile, install-callback; fcl: get/find/trace/stats."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import components, health, profile as profile_mod, retain, search, transfer
from .profile import Profile, ProfileError
from .records import zstd_binary
from .redact import Redactor, rule_from_config
from .ship import ship, zstd_compressor
from .store import open_store, sha256_file

CALLBACK_SOURCE = Path(__file__).with_name("proxy_callback.py")


def _redactor(p: Profile) -> Redactor:
    return Redactor(extra=[rule_from_config(r) for r in p.redaction_rules], builtin=p.builtin_redaction)


def _notify(p: Profile, events: list[dict]) -> None:
    if not events or not p.notifiers:
        return
    health.dispatch(events, components.build_all("notify", p), p.notify_min_severity)


def cmd_ship(p: Profile, _args: argparse.Namespace) -> int:
    store = open_store(p.store_type, p.store_path, p.store_host)
    compress = zstd_compressor(p.zstd or zstd_binary(), p.zstd_level)
    report = ship(p.spool_path(), store, datetime.now(timezone.utc), compress, _redactor(p),
                  grace=timedelta(minutes=p.ship_grace_minutes))
    print(f"ship: {report.shipped} shipped, {report.failed} failed, {report.bytes_raw} B raw -> "
          f"{report.bytes_compressed} B zst, {report.redactions} redactions, "
          f"{report.unparseable_lines} unparseable line(s)", flush=True)
    events = []
    if report.failed:
        events.append(health.event("ship_failed", "critical", f"{report.failed} spool file(s) failed to ship; "
                                   f"first: {report.failures[0][:200]}", failed=report.failed))
    if report.unparseable_lines:
        events.append(health.event("unparseable_lines", "warning",
                                   f"{report.unparseable_lines} spool line(s) were not valid JSON",
                                   lines=report.unparseable_lines))
    _notify(p, events)
    return 1 if report.failed else 0


def cmd_retain(p: Profile, args: argparse.Namespace) -> int:
    store = open_store(p.store_type, p.store_path, p.store_host)
    now = datetime.now(timezone.utc)
    days = store.usage()
    plan = retain.plan_deletions(days, now.date(), p.max_age_days, p.max_bytes)
    total = sum(d.bytes for d in days)
    print(f"retain: {len(days)} day dirs, {total} B; policy {p.max_age_days} d / {p.max_bytes} B; "
          f"{len(plan)} to delete{'' if args.apply else ' (dry run)'}", flush=True)
    if not args.apply:
        for d in plan:
            print(f"would delete {d.usage.rel_path} bytes={d.usage.bytes} reason={d.reason}")
        return 0
    lines = retain.apply(store, plan, now)
    if lines:
        _notify(p, [health.event("retention_deleted", "info", f"retention deleted {len(lines)} day dir(s)",
                                 days=len(lines), bytes=sum(d.usage.bytes for d in plan))])
    return 0


def cmd_health(p: Profile, _args: argparse.Namespace) -> int:
    events = health.check(p.spool_path(), p.spool_cap_bytes, datetime.now(timezone.utc),
                          timedelta(minutes=p.ship_grace_minutes))
    for ev in events:
        print(f"{ev['severity']}: {ev['kind']}: {ev['summary']}", flush=True)
    _notify(p, events)
    print(f"health: {len(events)} event(s)", flush=True)
    return 0


def cmd_export(p: Profile, args: argparse.Namespace) -> int:
    if args.jsonl:
        exporters = [components.JsonlExporter(args.jsonl)]
    else:
        exporters = components.build_all("export", p)
        if not exporters:
            raise ProfileError("no [[export]] entries in the profile; pass --jsonl PATH for a one-off export")
    results = transfer.export(p.search_roots(), _redactor(p), exporters, args.since, args.until)
    print(json.dumps(results), file=sys.stderr)
    return 0


def cmd_import(p: Profile, args: argparse.Namespace) -> int:
    if args.jsonl:
        sources = [(f"jsonl:{args.jsonl}", components.JsonlImporter(args.jsonl))]
    else:
        built = components.build_all("import", p)
        if not built:
            raise ProfileError("no [[import]] entries in the profile; pass --jsonl PATH for a one-off import")
        sources = [(f"{c.type}:{c.options.get('path') or c.options.get('entry', '')}", imp)
                   for c, imp in zip(p.imports, built)]
    status = 0
    for name, importer in sources:
        report = transfer.import_into_spool(importer.records(), p.spool_path(), name)
        print(f"import {name}: {report.imported} imported, {report.rejected} rejected", flush=True)
        for problem in report.problems:
            print(f"  {problem}")
        status |= 1 if report.rejected else 0
    return status


def cmd_profile(p: Profile | None, args: argparse.Namespace) -> int:
    if args.action == "import":
        source = Path(args.file).expanduser()
        candidate = profile_mod.from_dict(profile_mod.read_file(source), source=str(source))
        if candidate.allow_plugins:
            print("note: this profile enables plugins; review every type = 'plugin' entry before using it",
                  file=sys.stderr)
        dest = profile_mod.resolve_path(args.profile)
        if dest.exists() and not args.force:
            raise ProfileError(f"{dest} exists; pass --force to replace it (a backup is kept)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            backup = dest.with_name(f"{dest.name}.bak-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            shutil.copy2(dest, backup)
            if sha256_file(backup) != sha256_file(dest):
                raise ProfileError(f"backup {backup} does not match {dest}; nothing replaced")
            print(f"backed up {dest} -> {backup}", file=sys.stderr)
        shutil.copyfile(source, dest)
        print(f"installed {source} -> {dest}", file=sys.stderr)
        return 0
    assert p is not None
    raw = profile_mod.to_raw(p)
    text = json.dumps(raw, indent=2)
    if args.action == "export" and args.output:
        Path(args.output).expanduser().write_text(text + "\n")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_install_callback(args: argparse.Namespace) -> int:
    target_dir = Path(args.config_dir).expanduser()
    if not target_dir.is_dir():
        raise ProfileError(f"{target_dir} is not a directory (pass the directory holding your LiteLLM config)")
    target = target_dir / "content_ledger.py"
    if target.exists():
        if sha256_file(target) == sha256_file(CALLBACK_SOURCE):
            print(f"{target} is already current", file=sys.stderr)
            return 0
        backup = target.with_name(f"content_ledger.py.bak-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        shutil.copy2(target, backup)
        if sha256_file(backup) != sha256_file(target):
            raise ProfileError(f"backup {backup} does not match {target}; nothing replaced")
        print(f"backed up {target} -> {backup}", file=sys.stderr)
    shutil.copyfile(CALLBACK_SOURCE, target)
    print(f"installed {target}; add \"content_ledger.proxy_handler_instance\" to litellm_settings.callbacks "
          f"and restart the proxy", file=sys.stderr)
    return 0


def _search_args(sub: argparse._SubParsersAction) -> None:
    g = sub.add_parser("get", help="one record by call id or response id")
    g.add_argument("ident")
    f = sub.add_parser("find", help="filter records")
    for opt in ("since", "until", "model", "caller", "status", "text"):
        f.add_argument(f"--{opt}")
    f.add_argument("--limit", type=int, default=50)
    f.add_argument("--json", action="store_true", help="full records as JSON lines")
    t = sub.add_parser("trace", help="every call of one session/trace, in time order")
    t.add_argument("session")
    t.add_argument("--json", action="store_true")
    s = sub.add_parser("stats")
    s.add_argument("--days", type=int)


def cmd_search(p: Profile, args: argparse.Namespace) -> int:
    roots = p.search_roots()
    if args.cmd == "get":
        rec = search.get(roots, args.ident)
        if rec is None:
            print(f"not found: {args.ident}", file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "stats":
        print(json.dumps(search.stats(roots, args.days), indent=2))
        return 0
    hits = (search.find(roots, args.since, args.until, args.model, args.caller, args.status, args.text, args.limit)
            if args.cmd == "find" else search.trace(roots, args.session))
    for rec in hits:
        print(json.dumps(rec, ensure_ascii=False) if args.json else search.summary(rec))
    return 0


def _parser(prog: str, search_only: bool) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog)
    ap.add_argument("--profile", help="profile path (default $CONTENT_LEDGER_PROFILE or "
                                      "~/.config/content-ledger/profile.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _search_args(sub)
    if search_only:
        return ap
    sub.add_parser("ship", help="redact, compress and store closed spool hours")
    r = sub.add_parser("retain", help="apply the retention policy to the store")
    r.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    sub.add_parser("health", help="check counters, spool size and shipping; notify")
    e = sub.add_parser("export", help="send redacted records to the profile's exporters")
    e.add_argument("--since")
    e.add_argument("--until")
    e.add_argument("--jsonl", help="one-off export to this file ('-' for stdout) instead of the profile's")
    i = sub.add_parser("import", help="import records into the spool")
    i.add_argument("--jsonl", help="one-off import from this .jsonl or .jsonl.zst file")
    pr = sub.add_parser("profile", help="show, export or import a profile")
    pr.add_argument("action", choices=("show", "export", "import"))
    pr.add_argument("file", nargs="?", help="file to import")
    pr.add_argument("-o", "--output", help="export destination (default stdout)")
    pr.add_argument("--force", action="store_true", help="replace an existing profile (a backup is kept)")
    ic = sub.add_parser("install-callback", help="copy the proxy callback into a LiteLLM config directory")
    ic.add_argument("config_dir")
    return ap


def _run(args: argparse.Namespace) -> int:
    if args.cmd == "install-callback":
        return cmd_install_callback(args)
    if args.cmd == "profile" and args.action == "import":
        if not args.file:
            raise ProfileError("profile import needs a FILE")
        return cmd_profile(None, args)
    p = profile_mod.load(args.profile)
    handlers = {"ship": cmd_ship, "retain": cmd_retain, "health": cmd_health, "export": cmd_export,
                "import": cmd_import, "profile": cmd_profile}
    handler = handlers.get(args.cmd, cmd_search)
    return handler(p, args)


def main(argv: list[str] | None = None) -> int:
    args = _parser("content-ledger", search_only=False).parse_args(argv)
    try:
        return _run(args)
    except ProfileError as exc:
        print(f"content-ledger: {exc}", file=sys.stderr)
        return 2


def fcl_main(argv: list[str] | None = None) -> int:
    args = _parser("fcl", search_only=True).parse_args(argv)
    try:
        return _run(args)
    except ProfileError as exc:
        print(f"fcl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
