"""Secret redaction for serialized ledger lines. Runs in the ship/export process, never inside the proxy.

Measured: ~22 ms per 150 KB record, and Python's re holds the GIL, so inside the proxy even a background
thread stalled the event loop (+33 ms p50 on large requests).

Rules apply to a serialized JSON line. No built-in pattern can consume a bare quote, and escapes are consumed
whole, so the line stays valid JSON. Personal data is deliberately not redacted: the content is the record.
Formats: OpenAI-style sk-/rk-/pk- keys; PEM private-key blocks; RFC 6750 s2.1 Bearer tokens; AWS access key
id prefixes AKIA/ASIA; GitHub token prefixes gh[pousr]_ and github_pat_; Slack xox[abprs]- tokens; Google API
keys AIza...; RFC 7519 JWTs (base64url '{"' header encodes as eyJ); key/token query parameters; values
assigned to secret-named keys.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_ESCAPED_CHAR = r'(?:[^"\\]|\\.)'
_SECRET_WORDS = r"password|passwd|passphrase|secret|api[-_ ]?key|access[-_ ]?token|auth[-_ ]?token|private[-_ ]?key"


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


BUILTIN_RULES: tuple[Rule, ...] = (
    Rule(
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----" + _ESCAPED_CHAR + r'*?(?:-----END [A-Z ]*PRIVATE KEY-----|(?="))'
        ),
        "[REDACTED:private_key_block]",
    ),
    Rule(
        "secret_field",
        re.compile(
            r'(?i)("[A-Za-z0-9_-]*(?:' + _SECRET_WORDS + r'|authorization)"\s*:\s*")' + _ESCAPED_CHAR + r'{4,}?(?=")'
        ),
        r"\1[REDACTED:secret_field]",
    ),
    Rule("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "[REDACTED:bearer]"),
    Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), "[REDACTED:jwt]"),
    Rule("api_secret_key", re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"), "[REDACTED:api_secret_key]"),
    Rule("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED:aws_access_key_id]"),
    Rule(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
        "[REDACTED:github_token]",
    ),
    Rule("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
    Rule("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "[REDACTED:google_api_key]"),
    Rule(
        "url_key_param",
        re.compile(r'(?i)([?&](?:key|api_key|apikey|token|access_token)=)[^&\s"\\]{6,}'),
        r"\1[REDACTED:url_key_param]",
    ),
    Rule(
        "secret_assignment",
        re.compile(r'(?i)\b(' + _SECRET_WORDS + r')(\s*[:=]\s*)((?:\\")?)[^\s"\\,;&]{4,}'),
        r"\1\2\3[REDACTED:secret_assignment]",
    ),
)


def rule_from_config(entry: Mapping[str, Any]) -> Rule:
    """Profile rule: {name, pattern} replaces each match with [REDACTED:name].

    A pattern that can match a bare quote is rejected here; any rule that still corrupts a line is caught by
    Redactor.line_checked, which refuses to emit invalid JSON."""
    name = str(entry["name"])
    if not re.fullmatch(r"[a-z0-9_]{1,40}", name):
        raise ValueError(f"redaction rule name {name!r} must be 1-40 chars of a-z, 0-9, _")
    compiled = re.compile(str(entry["pattern"]))
    for probe in _QUOTE_PROBES:
        m = compiled.search(probe)
        if m and ('"' in m.group(0) or "\\" in m.group(0)):
            raise ValueError(f"redaction rule {name!r} can match a quote or backslash; anchor it to token characters")
    return Rule(name, compiled, f"[REDACTED:{name}]")


_QUOTE_PROBES = ('"', "\\", '\\"', 'a"', "a\\", 'x"', "x\\", '"x', "\\x", '0"', "0\\", 'A"', "A\\", ' "', " \\")


class Redactor:
    def __init__(self, extra: Iterable[Rule] = (), builtin: bool = True) -> None:
        self.rules: tuple[Rule, ...] = (BUILTIN_RULES if builtin else ()) + tuple(extra)

    def line(self, text: str) -> tuple[str, int]:
        total = 0
        for rule in self.rules:
            text, n = rule.pattern.subn(rule.replacement, text)
            total += n
        return text, total

    def line_checked(self, text: str) -> tuple[str, int]:
        """Redact a JSON line and prove it still parses; raises ValueError instead of emitting a broken line."""
        clean, n = self.line(text)
        try:
            json.loads(clean)
        except json.JSONDecodeError as exc:
            raise ValueError(f"redaction produced invalid JSON ({exc}); check custom redaction rules") from exc
        return clean, n

    def record(self, record: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        clean, n = self.line_checked(json.dumps(record, ensure_ascii=False))
        return json.loads(clean), n


def redact(text: str) -> tuple[str, int]:
    return Redactor().line(text)
