# Latency

Measured 2026-10-03 against LiteLLM 1.92.0 on one Linux host (6-core i7-3930K, Python 3.13).

## Method

- **Proxies:** four throwaway LiteLLM proxies bound to 127.0.0.1, each from the same config with no database
  and `mock_response` deployments. That isolates proxy-side overhead from model latency. They differ only in
  callbacks:

  | Arm | Callbacks |
  |---|---|
  | none | no callbacks |
  | base | a trivial logger that reads `standard_logging_object` scalars. A production proxy usually has callbacks already, so this is the honest baseline. |
  | ledger | base plus `content_ledger` |
  | base2 | base twice, a control for "any second callback" |

- **Requests:** 60 per arm per case. Arms are rotated on every request so drift hits all arms equally, after 5
  warm-ups each. Each request carries a unique nonce, and there is no cache.
- **Cases:** a small chat call, plus a ~151 KB prompt (the size of a ~37k-token agent context) sent
  non-streaming, streaming, and as a failure (`mock_response: litellm.InternalServerError`). On the failure
  path LiteLLM awaits callbacks before replying.

## Final design

Client-side milliseconds:

| Case | base p50 / p95 / p99 | ledger p50 / p95 / p99 | ledger minus base |
|---|---|---|---|
| small non-stream | 4.32 / 5.84 / 6.24 | 4.32 / 5.35 / 5.76 | +0.00 / -0.49 / -0.48 |
| large non-stream | 5.18 / 7.43 / 7.76 | 5.34 / 6.89 / 7.49 | +0.16 / -0.54 / -0.27 |
| large stream | 19.92 / 27.97 / 33.61 | 19.43 / 23.31 / 29.69 | -0.49 / -4.66 / -3.92 |
| large failure | 37.16 / 83.45 / 84.18 | 35.14 / 83.77 / 103.21 | -2.02 / +0.32 / +19.03 |

The base2 control moved between -2.7 and +7.2 ms at p50/p95 in the same run; that is the noise floor.

- At p50 and p95 the ledger is within noise on every path.
- The visible cost is the failure p99: about once per second the writer drains its batch, and a request in
  flight at that moment can wait for the GIL.

Handler cost, timed inside the real proxy on 151 KB records:

| Step | p50 |
|---|---|
| build record | 0.064 ms |
| serialize | 0.085 ms |
| enqueue | 0.026 ms |

## What the measurements ruled out

1. **Redaction inside the proxy.** Large requests paid +33 ms at p50 and +53 ms at p95, failures +93 ms,
   against a no-callback proxy.
   - Serializing a 151 KB record takes 0.026 ms; the redaction regexes take 21.9 ms.
   - Python's `re` holds the GIL, so a background thread still stalls the event loop.
   - Redaction runs in the ship process instead.
2. **Waking the writer per record.** With redaction gone, large failures still paid +13-19 ms. A second
   ordinary callback cost about 1 ms, and the handler itself 0.18 ms. Waking the writer on every submit made
   it compete for the GIL while the error response was being built. Draining on a 1 s timer removed the
   penalty at p50 and p95.

## Reproducing

The bench used a throwaway config with `mock_response` deployments, `num_retries: 0` and
`global_disable_no_log_param: true`, a trivial baseline `CustomLogger`, and a client that alternates arms per
request and records client-side wall time. Start the proxies on ports nothing else uses, and stop them by PID
afterwards. A leftover proxy on a reused port silently answers for the arm you meant to start.
