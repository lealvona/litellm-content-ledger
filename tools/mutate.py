"""Mutation check: break one guarantee at a time in the source, run the tests, expect a failure, restore.

Bytecode writing is disabled while mutants run: a same-length mutant restored within the same second can
otherwise leave a stale compiled mutant in __pycache__ that later runs execute silently.

Usage: python tools/mutate.py
"""

import os
import subprocess
import sys
from pathlib import Path

SRC = Path("src/content_ledger")
NO_PYC = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

MUTATIONS = [
    ("proxy_callback.py", "leak api_key into record", '"client_requested_no_log": params.get("no-log") is True,',
     '"client_requested_no_log": params.get("no-log") is True, "k": params.get("api_key"),'),
    ("proxy_callback.py", "ignore queue budget",
     "if self._stopped or self._pending_bytes + len(line) > self.queue_budget_bytes:", "if self._stopped:"),
    ("proxy_callback.py", "ignore spool cap", "            if self._spool_bytes + len(data) > self.spool_cap_bytes:\n",
     "            if False:\n"),
    ("proxy_callback.py", "drop root-device guard", "if self._root_device is not None and", "if False and"),
    ("proxy_callback.py", "submit wakes writer", "            self._pending_bytes += len(line)\n            return True",
     "            self._pending_bytes += len(line)\n            self._flush_requested = True\n"
     "            self._cond.notify_all()\n            return True"),
    ("proxy_callback.py", "spool not private", "self.spool.mkdir(mode=0o700, parents=True)", "self.spool.mkdir(parents=True)"),
    ("proxy_callback.py", "keep every header", "if isinstance(k, str) and (k.lower() in names", "if isinstance(k, str) and (True"),
    ("proxy_callback.py", "no status file", "            os.replace(tmp, status_dir / f\"{self._tag}.json\")", "            pass"),
    ("redact.py", "disable redaction", "            text, n = rule.pattern.subn(rule.replacement, text)", "            n = 0"),
    ("redact.py", "naive escape handling", "_ESCAPED_CHAR = r'(?:[^\"\\\\]|\\\\.)'", "_ESCAPED_CHAR = r'.'"),
    ("redact.py", "hyphenated secret names missed", "api[-_ ]?key", "api[_ ]?key"),
    ("redact.py", "accept quote-eating rules", "raise ValueError(f\"redaction rule {name!r} can match", "print(f\"x {name!r} can match"),
    ("ship.py", "ship skips redaction", "                clean, n = redactor.line_checked(line)", "                clean, n = line, 0"),
    ("ship.py", "delete without far-end check", "    if far_end != local_hash:\n        msg", "    if False:\n        msg"),
    ("ship.py", "overwrite differing store file", "    if existing is not None and existing != local_hash:", "    if False:"),
    ("ship.py", "ship the current hour", "if start is None or start + timedelta(hours=1) + grace > now:", "if start is None:"),
    ("retain.py", "today deletable by size", "        if total <= cap_bytes or d.day == today:", "        if total <= cap_bytes:"),
    ("retain.py", "age off by one", "(today - d.day).days > max_age_days", "(today - d.day).days >= max_age_days"),
    ("store.py", "no day path validation", "    m = DAY_PATH.match(rel_path)\n    if not m:", "    m = DAY_PATH.match(rel_path)\n    if False:"),
    ("components.py", "plugins on by default", "    if not profile.allow_plugins:", "    if False:"),
    ("transfer.py", "export unredacted", "        clean, _n = redactor.record(rec)", "        clean = rec"),
    ("transfer.py", "import skips validation", "            problems = validate(rec)", "            problems = []"),
    ("health.py", "re-report every run", '    if stale and not state.get("stale", False):', "    if stale:"),
    ("search.py", "trace ignores session header", "if session in session_ids(rec)", "if session == rec.get('trace_id')"),
    ("profile.py", "missing env var becomes empty", "            if value not in os.environ:", "            if False:"),
    ("cli.py", "install-callback without backup", "        shutil.copy2(target, backup)\n        if sha256_file(backup) != sha256_file(target):\n            raise",
     "        if False:\n            raise"),
]


def main() -> int:
    survivors = []
    for filename, name, old, new in MUTATIONS:
        target = SRC / filename
        original = target.read_text()
        count = original.count(old)
        if count != 1:
            print(f"SKIP {name}: pattern found {count} times in {target}")
            survivors.append(name)
            continue
        try:
            target.write_text(original.replace(old, new))
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
                                    capture_output=True, text=True, env=NO_PYC)
        finally:
            target.write_text(original)
        killed = result.returncode != 0
        print(f"{'KILLED' if killed else 'SURVIVED'} {name}")
        if not killed:
            survivors.append(name)
    print(f"{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
