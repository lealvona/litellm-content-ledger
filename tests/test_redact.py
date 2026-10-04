import json

import pytest

from content_ledger.redact import Redactor, redact, rule_from_config


def _line(text: str) -> str:
    return json.dumps({"messages": [{"role": "user", "content": text}]})


def _content(line: str) -> str:
    return json.loads(line)["messages"][0]["content"]


SECRETS = [
    ("api_secret_key", "use sk-proj_ABCDEFGHIJKLMNOP1234 now"),
    ("bearer", "Authorization: Bearer abcdefghijklmnop.qrstuv"),
    ("aws_access_key_id", "key AKIAABCDEFGHIJKLMNOP here"),
    ("github_token", "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123 x"),
    ("github_token", "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"),
    ("slack_token", "xoxb-123456789012-abcdefghijkl"),
    ("google_api_key", "AIzaSyA-1234567890abcdefghijklmnopqrstu"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcDEF123_-xyz"),
    ("url_key_param", "https://api.example.invalid/v1/models?key=SECRETVALUE123"),
    ("secret_assignment", "password=hunter2hunter2"),
    ("secret_assignment", "api_key: 9f8e7d6c5b4a"),
]


@pytest.mark.parametrize(("rule", "text"), SECRETS)
def test_each_rule_redacts_and_keeps_json_valid(rule: str, text: str):
    out, n = redact(_line(text))
    assert n >= 1
    assert f"[REDACTED:{rule}]" in _content(out)


def test_secret_field_in_structured_json():
    line = json.dumps({"headers": {"Authorization": "Basic dXNlcjpwYXNz", "x-api-key": "abcd1234efgh"}})
    out, n = redact(line)
    headers = json.loads(out)["headers"]
    assert headers == {"Authorization": "[REDACTED:secret_field]", "x-api-key": "[REDACTED:secret_field]"}


def test_private_key_block_with_escaped_newlines():
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY-----"
    out, n = redact(_line(f"here: {key} done"))
    assert n == 1
    assert _content(out) == "here: [REDACTED:private_key_block] done"


def test_unterminated_private_key_block_stays_valid_json():
    out, n = redact(_line('start -----BEGIN PRIVATE KEY-----\nMIIabc \\" quoted "inner" tail'))
    assert n == 1
    assert _content(out).startswith("start [REDACTED:private_key_block]")


def test_secret_value_next_to_escaped_quote_stays_valid_json():
    out, _ = redact(_line('password="hunter2hunter2" and "more"'))
    assert "hunter2hunter2" not in _content(out)


@pytest.mark.parametrize("text", ["please refresh the token later", "mail me at someone@example.com",
                                  "the secret to good soup is salt", "skip-this-word is fine"])
def test_benign_text_untouched(text: str):
    line = _line(text)
    out, n = redact(line)
    assert (out, n) == (line, 0)


def test_profile_rule_is_applied():
    r = Redactor(extra=[rule_from_config({"name": "employee_id", "pattern": r"\bEMP-\d{6}\b"})])
    out, n = r.line(_line("badge EMP-123456 at the door"))
    assert n == 1 and _content(out) == "badge [REDACTED:employee_id] at the door"


@pytest.mark.parametrize("pattern", [r".+", r"[^a]+", r"\"", r"x\\"])
def test_profile_rule_that_can_eat_quotes_is_rejected(pattern: str):
    with pytest.raises(ValueError):
        rule_from_config({"name": "bad", "pattern": pattern})


def test_profile_rule_name_is_validated():
    with pytest.raises(ValueError):
        rule_from_config({"name": "Bad Name!", "pattern": r"\bX\b"})


def test_line_checked_refuses_to_emit_broken_json():
    import re

    from content_ledger.redact import Rule

    broken = Redactor(extra=[Rule("evil", re.compile(r'"messages"'), "[REDACTED:evil]")], builtin=False)
    with pytest.raises(ValueError):
        broken.line_checked(_line("hello"))
