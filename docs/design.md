# Design

## Goal

Record the exact request and response of every call through a LiteLLM proxy. The record must be searchable
by call id, time, model, caller and conversation, without adding latency to requests, and kept within a fixed
retention window and size cap.

## Stages and where code runs

| Stage | Runs in | Sees | Configured by |
|---|---|---|---|
| Capture (`proxy_callback.py`) | the LiteLLM proxy process | full content, unredacted | environment variables only |
| Ship (`content-ledger ship`) | its own process, from a timer | spool files (unredacted), the store | profile |
| Retain (`content-ledger retain`) | its own process, from a timer | store directory listing only | profile |
| Health (`content-ledger health`) | its own process, from a timer | counters and sizes, never content | profile |
| Export / import | its own process, on demand | redacted records only | profile |
| Search (`fcl`) | interactive | spool (unredacted) and store | profile |

The capture side is deliberately fixed. It does not read the profile and loads no plugins. It never makes
network calls; it reports drops and errors by writing a small status file that `health` reads. Everything
configurable, and everything a plugin can touch, runs in a separate process.

## Capture

`ContentLedger.async_log_success_event` / `async_log_failure_event` do the following:

1. Build a record field by field from `kwargs["standard_logging_object"]`. `kwargs["api_key"]` (the raw
   upstream provider key, passed to every callback by LiteLLM) is never read.
2. Serialize it with orjson.
3. `submit()` it to an in-memory queue bounded by bytes.

LiteLLM awaits failure callbacks inline before it sends an error response, so this path must stay trivial.

The writer thread wakes on a timer, once a second by default. It does not wake per record. It appends each
line to `spool/YYYY-MM-DD/HH.<host>-<pid>.jsonl` (UTC hour). The spool directory is 0700. Writes are refused
when the spool is on the root filesystem, or when the spool is at its byte cap; such records are dropped and
counted. Counters are written to `spool/_status/<host>-<pid>.json`.

Why no redaction here: regex redaction of a 150 KB record took about 22 ms. Python's `re` holds the GIL, so
even from a background thread it stalled the proxy's event loop. Why no per-record wake-ups: they made the
writer compete for the GIL while LiteLLM was still building error responses. Both effects were measured; see
`latency.md`.

## Record schema (`v: 1`)

```json
{"v": 1, "ts_start": "ISO-8601 UTC", "ts_end": "...", "status": "success|failure",
 "call_id": "litellm_call_id (= x-litellm-call-id response header)", "id": "provider response id",
 "trace_id": "...", "call_type": "...", "stream": false, "cache_hit": false,
 "model": "...", "model_group": "...", "model_id": "...", "provider": "...", "api_base": "...",
 "caller": {"key_alias": "...", "key_hash": "...", "user_id": "...", "team_id": "...", "end_user": "...",
            "ip": "...", "user_agent": "..."},
 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0},
 "correlation": {"headers": {"x-litellm-session-id": "..."}, "requester_metadata": {}, "request_tags": []},
 "client_requested_no_log": false, "model_parameters": {}, "messages": [], "response": {}, "error": null}
```

`trace_id` follows LiteLLM's own precedence: `litellm_session_id`, `litellm_trace_id`, then
`metadata.session_id`, then `metadata.trace_id`. A client sets it with the `x-litellm-session-id` header.

## Ship

For each closed hour file (the hour has ended, plus a grace period, and the file has been idle at least 2
minutes):

1. Redact every line.
2. Compress with `zstd`.
3. Upload to the store.
4. Compare the sha256 at the far end.
5. Delete the spool copy only on a match.

A different file of the same name already in the store is never overwritten; the new file gets a hash
suffix.

## Store

| Type | Upload | Verify |
|---|---|---|
| `local` | file copy | local sha256 |
| `ssh` | rsync | remote `sha256sum`, run with `ssh -n` |

Layout: `YYYY/MM/DD/HH.*.jsonl.zst`. `_ops/retention.log` records every deletion.

## Retention

Delete day directories older than `max_age_days`. Then delete oldest days first until the total is at or
under `max_bytes`. Today is never deleted. Dry-run is the default; `--apply` deletes.

## Profiles

A profile is a TOML (or JSON) file holding every instance-specific value. `content-ledger profile export`
writes the normalized profile as JSON; `profile import` validates a file and installs it. See
`examples/profile.toml`.

## Export, import and plugins

- **Exporters** receive records after redaction:
  - `jsonl` writes a file or stdout.
  - `victorialogs` POSTs to `<url>/insert/jsonline`. Messages and response are sent as JSON strings, because
    VictoriaLogs flattens nested objects.
- **Importers** yield records. They are validated against schema v1, then written into the spool under their
  own hour, so the normal ship path redacts, compresses and stores them. Built-in: `jsonl`.
- **Notifiers** receive health events: kind, severity, summary and numeric attributes, never content.
  Built-in: `webhook` and `stderr`.

A profile entry with `type = "plugin"` names third-party code as `entry = "module:attribute"`. Plugins load
only when `[plugins] allow = true`, and only in the export, import and notify stages. See `plugins.md`.
