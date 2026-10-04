"""Where shipped files live: a local directory (or a mounted share) or a remote directory over ssh + rsync."""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

DAY_PATH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")


@dataclass(frozen=True, slots=True)
class DayUsage:
    day: date
    rel_path: str
    bytes: int


def validate_day_path(rel_path: str) -> date:
    m = DAY_PATH.match(rel_path)
    if not m:
        raise ValueError(f"not a day directory: {rel_path!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_du_line(line: str) -> DayUsage | None:
    parts = line.split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    try:
        day = validate_day_path(parts[1])
    except ValueError:
        return None
    return DayUsage(day=day, rel_path=parts[1], bytes=int(parts[0]) * 1024)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Store(Protocol):
    def upload(self, local: Path, rel_path: str) -> None: ...

    def sha256(self, rel_path: str) -> str | None: ...

    def usage(self) -> list[DayUsage]: ...

    def delete_day(self, rel_path: str, log_line: str) -> None: ...


class LocalStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser()

    def upload(self, local: Path, rel_path: str) -> None:
        dest = self.root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(local, tmp)
        tmp.chmod(0o600)
        tmp.replace(dest)

    def sha256(self, rel_path: str) -> str | None:
        p = self.root / rel_path
        return sha256_file(p) if p.is_file() else None

    def usage(self) -> list[DayUsage]:
        out = []
        for d in sorted(self.root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]")):
            rel = d.relative_to(self.root).as_posix()
            try:
                day = validate_day_path(rel)
            except ValueError:
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            out.append(DayUsage(day, rel, size))
        return out

    def delete_day(self, rel_path: str, log_line: str) -> None:
        validate_day_path(rel_path)
        target = self.root / rel_path
        shutil.rmtree(target)
        try:
            target.parent.rmdir()
        except OSError:
            pass
        ops = self.root / "_ops"
        ops.mkdir(exist_ok=True, mode=0o700)
        with open(ops / "retention.log", "a", encoding="utf-8") as fh:
            fh.write(log_line + "\n")


class SshStore:
    """rel paths come from this package (no spaces or shell metacharacters) and are quoted anyway.
    ssh runs with -n so it never consumes the caller's stdin."""

    def __init__(self, host: str, root: str) -> None:
        self.host = host
        self.root = root.rstrip("/")

    def _ssh(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", self.host, command],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def _ssh_ok(self, command: str) -> str:
        res = self._ssh(command)
        if res.returncode != 0:
            raise RuntimeError(f"ssh {self.host} failed ({res.returncode}): {res.stderr.strip()}")
        return res.stdout

    def upload(self, local: Path, rel_path: str) -> None:
        target = f"{self.root}/{rel_path}"
        parent = target.rsplit("/", 1)[0]
        self._ssh_ok(f"mkdir -p {shlex.quote(parent)} && chmod 700 {shlex.quote(self.root)}")
        subprocess.run(["rsync", "-t", "--chmod=F600", str(local), f"{self.host}:{shlex.quote(target)}"],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def sha256(self, rel_path: str) -> str | None:
        target = shlex.quote(f"{self.root}/{rel_path}")
        res = self._ssh(f"test -f {target} && sha256sum {target}")
        if res.returncode != 0 or not res.stdout:
            return None
        return res.stdout.split()[0]

    def usage(self) -> list[DayUsage]:
        out = self._ssh_ok(f"cd {shlex.quote(self.root)} && for d in [0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]; "
                           f"do [ -d \"$d\" ] && du -sk \"$d\"; done; true")
        return [u for u in (parse_du_line(line) for line in out.splitlines()) if u is not None]

    def delete_day(self, rel_path: str, log_line: str) -> None:
        validate_day_path(rel_path)
        target = f"{self.root}/{rel_path}"
        ops = f"{self.root}/_ops"
        self._ssh_ok(f"rm -rf -- {shlex.quote(target)} && "
                     f"{{ rmdir {shlex.quote(target.rsplit('/', 1)[0])} 2>/dev/null; true; }} && "
                     f"mkdir -p {shlex.quote(ops)} && echo {shlex.quote(log_line)} >> {shlex.quote(ops + '/retention.log')}")


def open_store(store_type: str, path: str, host: str | None) -> Store:
    if store_type == "local":
        return LocalStore(path)
    if store_type == "ssh" and host:
        return SshStore(host, path)
    raise ValueError(f"cannot open store type {store_type!r}")
