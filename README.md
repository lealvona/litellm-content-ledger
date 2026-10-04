# litellm-content-ledger

A flight recorder for a [LiteLLM](https://github.com/BerriAI/litellm) proxy. It keeps the exact request and
response of every call. Recording happens off the request path, the content is redacted for secrets and
compressed, and the store is kept within a retention window and a hard size cap. When an agent receives
something strange, you can read exactly what the provider sent.

An illustrated walkthrough is in [`content-ledger.html`](content-ledger.html): open it in a browser. It needs
no server.

## Why not LiteLLM's built-in prompt storage

`store_prompts_in_spend_logs` writes content into the proxy's Postgres `LiteLLM_SpendLogs` table. In LiteLLM
1.92.0 it truncates every string longer than `MAX_STRING_LENGTH_PROMPT_IN_DB`, which defaults to 2,048
characters and is adjustable through that environment variable. It also stores the full prompt uncompressed
on every turn of an agent conversation, in the same database the proxy depends on. The ledger keeps content
out of that database and compresses it, typically 5× per record and much more per hourly file.

## What it guarantees

Each property below has a test that fails without it (`tools/mutate.py` breaks them one at a time).

- **Logging never fails a request.** A full queue or a full spool drops the record and counts it. Every error
  is caught.
- **Provider keys are never recorded.** LiteLLM passes the raw upstream API key to every callback in
  `kwargs["api_key"]`. Records are built field by field and never read it.
- **Secrets are redacted before data leaves the spool.** Bearer tokens, `sk-` style keys, AWS, GitHub, Slack
  and Google keys, JWTs, private-key blocks and secret-named fields become `[REDACTED:<rule>]`. Personal data
  is kept on purpose; the content is the record.
- **Nothing is deleted until its copy is proven.** A spool file is removed only after its compressed copy's
  sha256 matches at the store. A different file of the same name is never overwritten.
- **The root filesystem is refused** for the spool by default. A full root disk takes databases down with it.
- **Retention is bounded twice:** by age and by total bytes, oldest day first. Every deletion is logged.

Measured overhead: within noise at p50 and p95 on every path, and +19 ms at p99 on failed requests. See
[`docs/latency.md`](docs/latency.md).

## Quick start

Requirements: Python 3.11 or newer for the tools, `zstd`, and `rsync` plus `ssh` if the store is remote. The
proxy-side callback uses only the standard library and orjson, which ships with LiteLLM.

```bash
pipx install git+https://github.com/lealvona/litellm-content-ledger
```

1. **Install the callback beside your LiteLLM config.** LiteLLM loads callback modules as files from the
   config's directory.

   ```bash
   content-ledger install-callback /path/to/litellm-config-dir
   ```

2. **Enable it in the LiteLLM config** and restart the proxy:

   ```yaml
   litellm_settings:
     callbacks: ["content_ledger.proxy_handler_instance"]   # keep your existing callbacks in the list
     global_disable_no_log_param: true                     # otherwise any client can send "no-log": true
   ```

   The callback reads only environment variables: `CONTENT_LEDGER_SPOOL` and the others listed at the top of
   `src/content_ledger/proxy_callback.py`.

3. **Write a profile.** Copy [`examples/profile.toml`](examples/profile.toml) to
   `~/.config/content-ledger/profile.toml` and set the store and retention.

4. **Install the timers** (systemd user units):

   ```bash
   cp deploy/systemd/content-ledger-* ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now content-ledger-ship.timer content-ledger-retain.timer content-ledger-health.timer
   ```

## Finding things

```bash
fcl get <x-litellm-call-id or provider response id>      # one call, full record
fcl find --since 2026-10-03T00:00:00Z --model my-model --caller my-key-alias --text "needle"
fcl trace <session id>                                   # every call of a conversation, in order
fcl stats --days 7
```

Clients link their calls into a conversation by sending `x-litellm-session-id: <id>`; it becomes the
record's `trace_id`. To keep other headers, set `CONTENT_LEDGER_CORRELATION_HEADERS` or
`CONTENT_LEDGER_CORRELATION_PREFIXES` (for example `x-myagent-`) in the proxy's environment.

## Export, import and profiles

```bash
content-ledger export --since 2026-10-01T00:00:00Z            # to the profile's [[export]] targets
content-ledger export --jsonl - | head                        # one-off, to stdout
content-ledger import --jsonl other-ledger.jsonl.zst          # validated, then shipped like any record
content-ledger profile export -o my-profile.json              # share a setup
content-ledger profile import my-profile.json                 # install one (an existing profile is backed up)
```

Exports are always redacted. The built-in exporters are `jsonl` and `victorialogs` (VictoriaLogs'
`/insert/jsonline`), the importer is `jsonl`, and the notifiers are `webhook` and `stderr`.

## Plugins

Third-party exporters, importers and notifiers can be loaded by name. This is **off by default**. Plugins
never load inside the proxy, and they only ever see redacted records or content-free health events. Read
[`docs/plugins.md`](docs/plugins.md) before enabling them.

## Storage sizing

Agents resend their whole conversation on every turn, so full records repeat context. Measured on one agent
deployment (3.3 bytes per token, zstd per record), full request plus response came to:

| Day | Compressed |
|---|---|
| Typical (~200 requests) | about 0.8 MB |
| Busiest (~1,550 requests, ~31 M prompt tokens) | about 20.5 MB |

Compressing each hourly file as a whole does better still. Set `max_bytes` from your own `fcl stats`.

## Encryption at rest

Not built in. The hook points are documented:

- in `ship.py`, after compression;
- in `records.open_records()` before reading.

Keep the key off the storage host.

## Compatibility

Developed and measured against LiteLLM 1.92.0. The callback relies on `CustomLogger.async_log_success_event`
/ `async_log_failure_event` and on fields of `standard_logging_object`. Re-run the tests and the latency
bench after upgrading LiteLLM.

## Licence

MIT. See [LICENSE](LICENSE).
