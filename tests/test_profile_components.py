import json
import textwrap
from pathlib import Path

import pytest

from content_ledger import components, profile
from content_ledger.profile import ProfileError
from fixtures import profile_dict, rec


def test_minimal_profile_loads_with_defaults(tmp_path: Path):
    p = profile.from_dict(profile_dict(tmp_path))
    assert (p.max_age_days, p.max_bytes, p.spool_cap_bytes) == (180, 10 * (1 << 30), 1 << 20)
    assert p.allow_plugins is False
    assert p.search_roots() == [tmp_path / "spool", tmp_path / "store"]


@pytest.mark.parametrize(("patch", "message"), [
    ({"store": {"type": "s3", "path": "x"}}, "type must be"),
    ({"store": {"type": "ssh", "path": "/x"}}, "host is required"),
    ({"retention": {"max_age_days": 0, "max_bytes": "1GiB"}}, "max_age_days"),
    ({"retention": {"max_age_days": 30}}, "max_bytes is required"),
    ({"retention": {"max_age_days": 30, "max_bytes": "lots"}}, "not a size"),
    ({"notify_settings": {"min_severity": "loud"}}, "min_severity"),
])
def test_invalid_profiles_fail_loudly(tmp_path: Path, patch, message):
    with pytest.raises(ProfileError, match=message):
        profile.from_dict(profile_dict(tmp_path, **patch))


def test_toml_file_and_round_trip(tmp_path: Path):
    path = tmp_path / "profile.toml"
    path.write_text(textwrap.dedent(f"""
        [ledger]
        spool = "{tmp_path / 'spool'}"
        [store]
        type = "ssh"
        host = "user@nas.example"
        path = "/srv/ledger"
        read_root = "/mnt/ledger"
        [retention]
        max_age_days = 90
        max_bytes = "5GiB"
        [[redaction.rules]]
        name = "employee_id"
        pattern = '\\bEMP-\\d{{6}}\\b'
        [[export]]
        type = "jsonl"
        path = "/tmp/out.jsonl"
        [[notify]]
        type = "webhook"
        url_env = "ALERT_URL"
    """))
    p = profile.load(str(path))
    assert p.search_roots() == [tmp_path / "spool", Path("/mnt/ledger")]
    again = profile.from_dict(json.loads(json.dumps(profile.to_raw(p))))
    assert again.to_dict() == p.to_dict()


def test_env_indirection_resolves_and_missing_var_fails(monkeypatch):
    monkeypatch.setenv("ALERT_URL", "http://alerts.invalid/hook")
    assert profile.resolve_env_options({"url_env": "ALERT_URL", "timeout_s": 3}) == {"url": "http://alerts.invalid/hook", "timeout_s": 3}
    monkeypatch.delenv("ALERT_URL")
    with pytest.raises(ProfileError, match="not set"):
        profile.resolve_env_options({"url_env": "ALERT_URL"})


# ---------------------------------------------------------------- components and plugins

PLUGIN_SOURCE = '''
class Recorder:
    seen = []
    def __init__(self, label="x"):
        self.label = label
    def notify(self, event):
        Recorder.seen.append((self.label, event["kind"]))

class NotANotifier:
    def __init__(self, **options):
        self.options = options
'''


def _plugin_profile(tmp_path: Path, allow: bool, entry: str = "my_plugin:Recorder") -> profile.Profile:
    (tmp_path / "plugins").mkdir(exist_ok=True)
    (tmp_path / "plugins" / "my_plugin.py").write_text(PLUGIN_SOURCE)
    return profile.from_dict(profile_dict(
        tmp_path, plugins={"allow": allow, "paths": [str(tmp_path / "plugins")]},
        notify=[{"type": "plugin", "entry": entry, "label": "L"}]))


def test_plugins_are_refused_by_default(tmp_path: Path):
    with pytest.raises(ProfileError, match="allow = true"):
        components.build_all("notify", _plugin_profile(tmp_path, allow=False))


def test_allowed_plugin_loads_with_options(tmp_path: Path):
    [notifier] = components.build_all("notify", _plugin_profile(tmp_path, allow=True))
    notifier.notify({"kind": "k", "severity": "warning", "summary": "s", "attrs": {}})
    assert notifier.label == "L"
    assert ("L", "k") in type(notifier).seen


@pytest.mark.parametrize(("entry", "message"), [
    ("my_plugin:NotANotifier", "no notify"), ("my_plugin:Missing", "no attribute"),
    ("no_such_module:X", "could not be imported"), ("badentry", "module:attribute")])
def test_bad_plugin_entries(tmp_path: Path, entry, message):
    with pytest.raises(ProfileError, match=message):
        components.build_all("notify", _plugin_profile(tmp_path, allow=True, entry=entry))


def test_unknown_builtin_type(tmp_path: Path):
    p = profile.from_dict(profile_dict(tmp_path, export=[{"type": "s3"}]))
    with pytest.raises(ProfileError, match="unknown export type"):
        components.build_all("export", p)


def test_victorialogs_exporter_posts_flat_lines(tmp_path: Path):
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def opener(req, timeout):
        sent.append((req.full_url, req.data.decode(), req.headers))
        return Resp()

    exporter = components.VictoriaLogsExporter("http://vl.invalid:9428/", batch=2, opener=opener)
    n = exporter.export([rec("c1", "2026-10-03T08:00:00+00:00", "s"), rec("c2", "2026-10-03T08:01:00+00:00", "s"),
                         rec("c3", "2026-10-03T08:02:00+00:00", "s")])
    assert n == 3 and len(sent) == 2
    url, body, headers = sent[0]
    assert url.startswith("http://vl.invalid:9428/insert/jsonline?")
    assert "_stream_fields=model_group%2Ckey_alias" in url
    first = json.loads(body.splitlines()[0])
    assert first["_time"] == "2026-10-03T08:00:00+00:00" and first["call_id"] == "c1"
    assert json.loads(first["messages_json"])[0]["content"] == "hello"
    assert all(not isinstance(v, (dict, list)) for v in first.values())
