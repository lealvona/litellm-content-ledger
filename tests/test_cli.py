import json
from pathlib import Path

import pytest

from content_ledger import cli


def _write_profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(f'[ledger]\nspool = "{tmp_path / "spool"}"\n[store]\ntype = "local"\npath = "{tmp_path / "store"}"\n'
                    f'[retention]\nmax_age_days = 30\nmax_bytes = "1GiB"\n')
    return path


def test_install_callback_backs_up_a_different_file(tmp_path: Path):
    target = tmp_path / "content_ledger.py"
    target.write_text("# older version\n")
    assert cli.main(["install-callback", str(tmp_path)]) == 0
    assert target.read_text() == cli.CALLBACK_SOURCE.read_text()
    backups = list(tmp_path.glob("content_ledger.py.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == "# older version\n"
    assert cli.main(["install-callback", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("content_ledger.py.bak-*"))) == 1


def test_profile_export_then_import_round_trip(tmp_path: Path, capsys):
    src = _write_profile(tmp_path)
    out = tmp_path / "exported.json"
    assert cli.main(["--profile", str(src), "profile", "export", "-o", str(out)]) == 0
    dest = tmp_path / "other" / "profile.json"
    assert cli.main(["--profile", str(dest), "profile", "import", str(out)]) == 0
    assert json.loads(dest.read_text()) == json.loads(out.read_text())
    assert cli.main(["--profile", str(dest), "profile", "import", str(out)]) == 2
    assert cli.main(["--profile", str(dest), "profile", "import", str(out), "--force"]) == 0
    assert len(list(dest.parent.glob("profile.json.bak-*"))) == 1


def test_missing_profile_is_a_clear_error(tmp_path: Path, capsys):
    assert cli.main(["--profile", str(tmp_path / "nope.toml"), "health"]) == 2
    assert "no profile at" in capsys.readouterr().err


def test_retain_dry_run_deletes_nothing(tmp_path: Path, capsys):
    profile = _write_profile(tmp_path)
    old = tmp_path / "store" / "2020/01/01"
    old.mkdir(parents=True)
    (old / "08.h-1.jsonl.zst").write_bytes(b"x")
    assert cli.main(["--profile", str(profile), "retain"]) == 0
    assert "would delete 2020/01/01" in capsys.readouterr().out
    assert old.exists()
    assert cli.main(["--profile", str(profile), "retain", "--apply"]) == 0
    assert not old.exists()


@pytest.mark.parametrize("argv", [["get", "x"], ["stats"]])
def test_fcl_entry_point_runs_searches(tmp_path: Path, argv):
    profile = _write_profile(tmp_path)
    code = cli.fcl_main(["--profile", str(profile), *argv])
    assert code in (0, 1)
