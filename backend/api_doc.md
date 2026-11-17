# inferq API reference

This document covers every HTTP endpoint in the project, with request and response fields, status codes, and a ready-to-run `curl` command for each.

| Service | Default base URL | Start command (from `backend/`) |
|---|---|---|
| **Control plane** (the system you call) | `http://127.0.0.1:8000` | `python -m inferq.api --port 8000 --provider-url http://127.0.0.1:9000` |
| **Provider simulator** (the fake LLM provider) | `http://127.0.0.1:9000` | `python -m simulator.server --port 9000` |
| **Mock callback receiver** (a fake client webhook) | `http://127.0.0.1:9100` | `python -m tools.mock_receiver --port 9100 --reject-first 2` |

To start all three at once, run `python -m tools.up` (add `--dashboard` for the React UI on :5173).
Interactive Swagger docs are also served by FastAPI at `http://127.0.0.1:8000/docs` and `http://127.0.0.1:9000/docs`.

---

## Using these requests in Postman

1. In Postman, click **Import**, choose **Raw text**, paste any `curl` command below, then click **Continue** and **Import**. Postman creates the request with the method, URL, headers and body already filled in.
2. Optionally, create an environment with these variables and replace the hosts in the URLs with `{{api}}`, `{{sim}}` and `{{rcv}}`:
   - `api` = `http://127.0.0.1:8000`
   - `sim` = `http://127.0.0.1:9000`
   - `rcv` = `http://127.0.0.1:9100`
3. Every request body is JSON. Postman needs the header `Content-Type: application/json`, which every curl command below already includes.

> **Windows note.** The curl commands use single quotes, which work in Git Bash, WSL, macOS and Linux. In PowerShell, use `curl.exe` and wrap the JSON in double quotes with escaped inner quotes. Importing into Postman avoids the issue entirely.

---

## Concepts used throughout

### Request states

| State | Meaning | Final? |
|---|---|---|
| `waiting` | Admitted. The request is queued for a send slot under the model's RPM/TPM limits, or waiting out a retry backoff. | no |
| `in_flight` | Sent to the provider. The system is waiting for its answer. | no |
| `succeeded` | The provider returned success. | **yes** (counts as *completed*) |
| `failed` | The provider returned a permanent error, or retries were exhausted. | **yes** (counts as *completed*) |
| `expired` | No send slot was available before the request's deadline, for example after a limit drop. | **yes** |
| `rejected` | Refused at admission: it could not be scheduled before its deadline, or the model's limit is 0 or too small for it. | **yes** |

Invariant: `submitted = waiting + in_flight + succeeded + failed + expired + rejected`, per model and in total.

### Deadlines and overload

Each request has a deadline: `deadline_s` from the request, or the model default, which is **60 s**. On admission the system estimates when the request could be sent. If that time is after the deadline, the request is rejected immediately with **HTTP 429** and a `retry_after_s` hint, rather than queuing without bound.

### Limits

Limits are a strict **rolling 60-second window**. No 60 s period ever exceeds the RPM or TPM limit. The dispatcher keeps a 1% guard below each limit, so `effective_rpm = floor(rpm × 0.99)`.

### Timestamps

All timestamps are Unix epoch seconds (float), for example `1794417950.153`.

---

# 1. Control plane (`:8000`)

## 1.1 Submit a single request

`POST /v1/requests`

Submits one inference request. The call returns immediately with the request's admission result; the provider call happens in the background.

**Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | string | yes | The model name, for example `model-a`. It must exist (see `GET /v1/models`). |
| `estimated_tokens` | integer ≥ 1 | yes | Estimated tokens, used to reserve TPM capacity. The reservation is corrected to the actual count afterwards. |
| `payload` | any JSON | no | Opaque request content (prompt and so on). It is forwarded to the provider. |
| `request_id` | string | no | Your own id. It is echoed back as `client_request_id`. |
| `deadline_s` | number > 0 | no | Seconds the request may wait for a send slot. Default 60. |
| `intended_at` | number | no | Epoch time at which you *meant* to send the request. Latency is measured from this time (used by the load generator). Defaults to the arrival time. |

**Responses**

| Status | When | Body |
|---|---|---|
| `202 Accepted` | Admitted (state `waiting`) | `{request_id, client_request_id, state, eta_s, status_url}` |
| `429 Too Many Requests` | Rejected at admission (no capacity before the deadline) | `{request_id, client_request_id, state: "rejected", reason, retry_after_s, eta_s: null, status_url}`. A `Retry-After` header is included when a retry time is known. |
| `404 Not Found` | Unknown model | `{"detail": "unknown model 'x'"}` |
| `422 Unprocessable Entity` | Invalid body, for example a missing field or `estimated_tokens` < 1 | FastAPI validation error |

Possible `reason` values on rejection are `no_capacity_before_deadline` and `exceeds_model_limit`.

```bash
curl -s -X POST http://127.0.0.1:8000/v1/requests \
  -H 'Content-Type: application/json' \
  -d '{
        "model": "model-a",
        "estimated_tokens": 1000,
        "payload": {"prompt": "Summarise this paragraph..."},
        "request_id": "my-req-001",
        "deadline_s": 60
      }'
```

Example `202` response:

```json
{
  "request_id": "r-816b9ba24a6f",
  "client_request_id": "my-req-001",
  "state": "waiting",
  "eta_s": 0.001,
  "status_url": "/v1/requests/r-816b9ba24a6f"
}
```

Example `429` response:

```json
{
  "request_id": "r-0c1d2e3f4a5b",
  "client_request_id": null,
  "state": "rejected",
  "eta_s": null,
  "status_url": "/v1/requests/r-0c1d2e3f4a5b",
  "reason": "no_capacity_before_deadline",
  "retry_after_s": 64.2
}
```

To force a rejection for testing, give a very small deadline while the model is saturated, or set the model's RPM to 0 (see §1.8).

```bash
curl -s -X POST http://127.0.0.1:8000/v1/requests -H 'Content-Type: application/json' \
  -d '{"model": "model-a", "estimated_tokens": 1000, "deadline_s": 0.001}'
```

---

## 1.2 Get a single request

`GET /v1/requests/{request_id}`

Returns the current state of one request and, once it is final, its result.

| Response field | Description |
|---|---|
| `request_id` / `client_request_id` | The system id and your id. |
| `model`, `batch_id` | The model, and the batch id if the request came from a batch (otherwise `null`). |
| `state` | See *Request states*. |
| `attempts` | How many times it was sent to the provider (retries included). |
| `estimated_tokens` / `actual_tokens` | The reserved estimate and the provider-metered actual count (set on success). |
| `output` | The provider output, on success. |
| `error` | The provider error body (on failure) or retry information (on rejection). |
| `reason` | Why the request ended as it did: `permanent_provider_error`, `attempts_exhausted`, `deadline_passed_before_send`, `limit_reduced_slot_past_deadline`, `deadline_passed_during_retry`, `no_capacity_before_deadline`, `exceeds_model_limit`, or `unknown_model`. |
| `created_at`, `eta`, `finished_at` | Epoch times. `eta` is the projected send time. |
| `latency_ms` | From `intended_at` to the final state. Only set for completed requests. |

Status codes: `200`, or `404` for an unknown id.

```bash
curl -s http://127.0.0.1:8000/v1/requests/r-816b9ba24a6f
```

```json
{
  "request_id": "r-816b9ba24a6f",
  "client_request_id": "my-req-001",
  "model": "model-a",
  "batch_id": null,
  "state": "succeeded",
  "attempts": 1,
  "estimated_tokens": 1000,
  "actual_tokens": 976,
  "output": "simulated completion for r-816b9ba24a6f",
  "error": null,
  "reason": null,
  "created_at": 1794417950.15,
  "eta": 1794417950.151,
  "finished_at": 1794417950.33,
  "latency_ms": 180.4
}
```

> Finished stand-alone requests are kept in memory. During very long runs (over 500k requests), finished requests older than 15 minutes are evicted. Batch results are stored durably (see §1.5).

---

## 1.3 Submit an asynchronous batch

`POST /v1/batches`

Accepts up to **100,000** requests at once. The call returns a batch id immediately, typically in well under one second, and processes the requests in the background. If you give a `callback_url`, the system POSTs the batch summary to it **after every request in the batch has reached a final state**, retrying until it succeeds.

**Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `requests` | array of request objects (same fields as §1.1) | yes | 1 to 100,000 items. |
| `callback_url` | string (URL) | no | Where to POST the completion callback (see §1.6). |
| `deadline_s` | number > 0 | no | Default deadline for every item. A `deadline_s` on an individual item overrides it. |

**Response:** `202 Accepted`

| Field | Description |
|---|---|
| `batch_id` | The id to poll. |
| `status` | Always `processing` at this point. |
| `total` | The number of requests accepted into the batch. |
| `status_url`, `results_url` | Relative URLs for polling and for fetching results. |
| `accepted_at` | Epoch time of acceptance. |

Status codes: `202`, or `422` for an invalid body (for example an empty list or more than 100k items). Items with an unknown model do **not** fail the batch; they are recorded as `rejected` with reason `unknown_model`.

```bash
curl -s -X POST http://127.0.0.1:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "callback_url": "http://127.0.0.1:9100/callback",
        "deadline_s": 120,
        "requests": [
          {"model": "model-a", "estimated_tokens": 1000, "payload": {"prompt": "item 1"}, "request_id": "client-1"},
          {"model": "model-b", "estimated_tokens": 800,  "payload": {"prompt": "item 2"}, "request_id": "client-2"},
          {"model": "model-a", "estimated_tokens": 1200, "payload": {"prompt": "item 3"}, "request_id": "client-3"}
        ]
      }'
```

```json
{
  "batch_id": "b-dd08e6384bb8",
  "status": "processing",
  "total": 3,
  "status_url": "/v1/batches/b-dd08e6384bb8",
  "results_url": "/v1/batches/b-dd08e6384bb8/results",
  "accepted_at": 1794418001.42
}
```

To generate a large test batch, for example 10,000 items, use Python and save the result to `batch.json`, then send it with curl or paste it into Postman:

```bash
python -c "import json;print(json.dumps({'callback_url':'http://127.0.0.1:9100/callback','requests':[{'model':'model-a' if i%2==0 else 'model-b','estimated_tokens':1000,'request_id':f'client-{i}'} for i in range(10000)]}))" > batch.json
curl -s -X POST http://127.0.0.1:8000/v1/batches -H 'Content-Type: application/json' --data-binary @batch.json
```

---

## 1.4 Get batch status

`GET /v1/batches/{batch_id}`

**While processing**, the response shows live counts:

| Field | Description |
|---|---|
| `batch_id`, `status` | `status` is `processing`. |
| `total`, `successful`, `failed` | `failed` is the sum of the failed, expired and rejected counts. |
| `counts` | `{succeeded, failed, expired, rejected}` |
| `remaining` | Requests not yet final. |
| `created_at`, `sealed_at` (`null`), `duration_s` (`null`), `results_url` | |
| `callback` | `{status: "awaiting_seal", attempts: 0, history: []}` if a callback URL was given, otherwise `null`. |

**Once sealed**, meaning every request is final, the response is the **immutable sealed summary**. This is exactly the body that is POSTed to `callback_url`, plus a `callback` block:

| Field | Description |
|---|---|
| `status` | `completed` (no failures) or `completed_with_errors`. |
| `total`, `successful`, `failed`, `counts` | Final counts. |
| `created_at`, `sealed_at`, `duration_s` | Timing from submission to seal. |
| `results_url` | Absolute URL of the paged results. |
| `summary_sha256` | A hash of the summary, which you can use to verify the callback body. |
| `callback` | Delivery state: `{url, status: pending/delivered/failed, attempts, next_attempt_at, delivered_at, history: [{attempt, at, status_code, error, elapsed_ms}]}` |

Other possible `status` values: `interrupted`, when the service restarted while the batch was processing, because request-level state is held in memory.

Status codes: `200`, or `404` for an unknown id.

```bash
curl -s http://127.0.0.1:8000/v1/batches/b-dd08e6384bb8
```

```json
{
  "batch_id": "b-dd08e6384bb8",
  "status": "completed_with_errors",
  "total": 10000,
  "successful": 9788,
  "failed": 212,
  "counts": {"succeeded": 9788, "failed": 212, "expired": 0, "rejected": 0},
  "created_at": 1794418001.42,
  "sealed_at": 1794418018.36,
  "duration_s": 16.94,
  "results_url": "http://127.0.0.1:8000/v1/batches/b-dd08e6384bb8/results",
  "summary_sha256": "b08d82857d05...",
  "callback": {
    "url": "http://127.0.0.1:9100/callback",
    "status": "delivered",
    "attempts": 3,
    "next_attempt_at": 1794418020.1,
    "delivered_at": 1794418021.77,
    "history": [
      {"attempt": 1, "at": 1794418018.58, "status_code": 503, "error": null, "elapsed_ms": 4.1},
      {"attempt": 2, "at": 1794418019.67, "status_code": 503, "error": null, "elapsed_ms": 3.2},
      {"attempt": 3, "at": 1794418021.77, "status_code": 200, "error": null, "elapsed_ms": 3.5}
    ]
  }
}
```

---

## 1.5 Get batch results (paged)

`GET /v1/batches/{batch_id}/results?offset=0&limit=1000`

| Query parameter | Default | Range | Description |
|---|---|---|---|
| `offset` | 0 | ≥ 0 | The index of the first result. |
| `limit` | 1000 | 1–20000 | The page size. |

Results are available once the batch is sealed. Before that, the response contains an empty `results` list and a `note`. Each result has the same shape as §1.2. Every request id in the batch appears **exactly once**, in submission order.

| Response field | Description |
|---|---|
| `batch_id`, `total`, `offset` | |
| `next_offset` | The offset of the next page, or `null` on the last page. |
| `results` | An array of request objects (see §1.2). |

```bash
curl -s "http://127.0.0.1:8000/v1/batches/b-dd08e6384bb8/results?offset=0&limit=2"
```

```json
{
  "batch_id": "b-dd08e6384bb8",
  "total": 10000,
  "offset": 0,
  "next_offset": 2,
  "results": [
    {"request_id": "r-1a2b...", "client_request_id": "client-0", "model": "model-a", "batch_id": "b-dd08e6384bb8",
     "state": "succeeded", "attempts": 1, "estimated_tokens": 1000, "actual_tokens": 912, "output": "simulated completion for r-1a2b...",
     "error": null, "reason": null, "created_at": 1794418001.43, "eta": 1794418001.43, "finished_at": 1794418001.61, "latency_ms": 182.3},
    {"request_id": "r-3c4d...", "client_request_id": "client-1", "model": "model-b", "batch_id": "b-dd08e6384bb8",
     "state": "failed", "attempts": 1, "estimated_tokens": 1000, "actual_tokens": null, "output": null,
     "error": {"error": "invalid_request", "request_id": "r-3c4d..."}, "reason": "permanent_provider_error",
     "created_at": 1794418001.43, "eta": 1794418001.43, "finished_at": 1794418001.6, "latency_ms": 171.0}
  ]
}
```

---

## 1.6 The completion callback (sent by the system to your `callback_url`)

This is not an endpoint you call. It is what the system sends to the `callback_url` you supplied in §1.3.

- **When:** once, after the batch is sealed, which means every request has reached `succeeded`, `failed`, `expired` or `rejected`.
- **Method:** `POST`, with header `Content-Type: application/json`.
- **Headers:**
  - `Idempotency-Key: <batch_id>`. Delivery is at-least-once, so use this header to deduplicate.
  - `X-Inferq-Batch-Id: <batch_id>`
  - `X-Inferq-Attempt: <n>`
- **Body:** the sealed summary. It is identical to `GET /v1/batches/{id}` without the `callback` block.
- **Success:** any `2xx` response. Any other status code, a timeout (10 s) or a connection error counts as a failure.
- **Retries:** exponential backoff starting at 1 s (configurable with `--callback-backoff`), doubling each time with ±20% jitter and a 60 s cap, for up to 12 attempts. After the last attempt, `callback.status` becomes `failed`. Pending callbacks are stored in SQLite and survive a restart.

You can simulate what your endpoint receives with this request (the attempt number is illustrative):

```bash
curl -s -X POST http://127.0.0.1:9100/callback \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: b-demo123' -H 'X-Inferq-Batch-Id: b-demo123' -H 'X-Inferq-Attempt: 1' \
  -d '{"batch_id":"b-demo123","status":"completed","total":2,"successful":2,"failed":0,
       "counts":{"succeeded":2,"failed":0,"expired":0,"rejected":0},
       "created_at":1794418001.4,"sealed_at":1794418002.0,"duration_s":0.6,
       "results_url":"http://127.0.0.1:8000/v1/batches/b-demo123/results","summary_sha256":"..."}'
```

---

## 1.7 List batches

`GET /v1/batches?limit=50`

Returns the most recent batches, newest first, each in the §1.4 shape. `limit` can be at most 500.

```bash
curl -s "http://127.0.0.1:8000/v1/batches?limit=10"
```

```json
{"batches": [ { "batch_id": "b-dd08e6384bb8", "status": "completed_with_errors", "total": 10000, "...": "..." } ]}
```

---

## 1.8 Models and limits

### List models and their current limits

`GET /v1/models`

```bash
curl -s http://127.0.0.1:8000/v1/models
```

```json
{
  "model-a": {"rpm": 50000, "tpm": 100000000, "effective_rpm": 49500, "effective_tpm": 99000000, "deadline_s": 60.0},
  "model-b": {"rpm": 20000, "tpm": 40000000, "effective_rpm": 19800, "effective_tpm": 39600000, "deadline_s": 60.0}
}
```

### Change a model's limits live (no restart)

`PUT /v1/models/{model}/limits`

| Field | Type | Required | Description |
|---|---|---|---|
| `rpm` | integer ≥ 0 | yes | The new requests-per-minute limit. `0` pauses the model: every new request is rejected. |
| `tpm` | integer ≥ 0 | yes | The new tokens-per-minute limit. |
| `propagate_to_provider` | boolean | no (default `true`) | Also applies the change to the provider simulator, which keeps its own copy of the limits. Set it to `false` to change only the dispatcher's view. |

Behavior:

- **Lowering a limit:** usage already sent in the last 60 s still counts against the new limit, so the model may pause until the window drains. Waiting requests are re-scheduled, and any whose new slot falls after their deadline are expired immediately; `expired` reports how many.
- **Raising a limit:** takes effect on the next scheduling tick.
- **Other models:** unaffected.
- **Unknown model name:** creates the model with these limits.

| Response field | Description |
|---|---|
| `model`, `rpm`, `tpm` | The applied limits. |
| `expired` | Waiting requests expired because of the change. |
| `provider_update` | The simulator's HTTP status (`200`), an error string, or `null` if not propagated. |

```bash
# drop Model A to 5,000 RPM
curl -s -X PUT http://127.0.0.1:8000/v1/models/model-a/limits \
  -H 'Content-Type: application/json' \
  -d '{"rpm": 5000, "tpm": 60000000}'

# restore it
curl -s -X PUT http://127.0.0.1:8000/v1/models/model-a/limits \
  -H 'Content-Type: application/json' \
  -d '{"rpm": 30000, "tpm": 60000000}'
```

```json
{"model": "model-a", "rpm": 5000, "tpm": 60000000, "expired": 6681, "provider_update": 200}
```

---

## 1.9 Metrics

### Totals, accounting invariant and latency

`GET /v1/metrics`

| Field | Description |
|---|---|
| `totals` | `{submitted, waiting, in_flight, succeeded, failed, expired, rejected, completed, invariant_ok}` |
| `models.<name>` | The same counts per model, plus `rpm_limit`, `tpm_limit`, and `window_requests` / `window_tokens` (usage in the trailing 60 s). |
| `latency.<name>` / `latency._all` | `{count, p50_ms, p95_ms, p99_ms, max_ms, mean_ms}` for completed requests, measured from `intended_at`. |
| `uptime_s` | Seconds since start, or since the last reset. |
| `in_flight_connections` | Provider calls currently open. |

```bash
curl -s http://127.0.0.1:8000/v1/metrics
```

```json
{
  "totals": {"submitted": 19999, "waiting": 1471, "in_flight": 147, "succeeded": 18381, "failed": 0,
             "expired": 0, "rejected": 0, "completed": 18381, "invariant_ok": true},
  "models": {
    "model-a": {"submitted": 19999, "waiting": 1471, "in_flight": 147, "succeeded": 18381, "failed": 0, "expired": 0,
                "rejected": 0, "completed": 18381, "invariant_ok": true, "rpm_limit": 50000, "tpm_limit": 100000000,
                "window_requests": 18800, "window_tokens": 17391224}
  },
  "latency": {"model-a": {"count": 18381, "p50_ms": 2081.5, "p95_ms": 3856.9, "p99_ms": 4011.8, "max_ms": 4743.3, "mean_ms": 2101.2},
              "_all": {"count": 18381, "p50_ms": 2081.5, "p95_ms": 3856.9, "p99_ms": 4011.8, "max_ms": 4743.3, "mean_ms": 2101.2}},
  "uptime_s": 31.2,
  "in_flight_connections": 147
}
```

### Per-second time series

`GET /v1/metrics/timeseries?since=<epoch>&model=<name>`

| Query parameter | Default | Description |
|---|---|---|
| `since` | `0` | Return only points with `t` greater than this. Use the last `t` you received to poll incrementally. |
| `model` | all | Filter to one model. |

Each point is one model in one second:

| Field | Description |
|---|---|
| `t`, `model` | The sample time and the model name. |
| `rpm_limit`, `tpm_limit` | The limits in force at that second. |
| `window_requests`, `window_tokens` | Usage in the trailing 60 s. |
| `waiting`, `in_flight` | Queue depth and open provider calls. |
| `sent`, `sent_tokens`, `completed`, `succeeded`, `failed`, `expired`, `rejected`, `retries`, `rate_limited` | Counts during that second. |

`limit_events` lists every limit change as `{at, model, rpm, tpm}`. The last 30 minutes are retained.

```bash
curl -s "http://127.0.0.1:8000/v1/metrics/timeseries?since=0&model=model-a"
```

```json
{
  "points": [
    {"t": 1794417955.0, "model": "model-a", "rpm_limit": 50000, "tpm_limit": 100000000, "window_requests": 4120,
     "window_tokens": 3811200, "waiting": 210, "in_flight": 130, "sent": 830, "sent_tokens": 830000, "completed": 818,
     "succeeded": 818, "failed": 0, "expired": 0, "rejected": 0, "retries": 0, "rate_limited": 0}
  ],
  "limit_events": [{"at": 1794417950.0, "model": "model-a", "rpm": 50000, "tpm": 100000000}]
}
```

---

## 1.10 Admin and health

### Reset counters

`POST /v1/admin/reset`

Clears counters, latencies, the time series and finished requests so a fresh run starts from zero. Limits, queued work and batches are kept.

```bash
curl -s -X POST http://127.0.0.1:8000/v1/admin/reset
```

```json
{"ok": true, "at": 1794418500.12}
```

### Health check

`GET /health`

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{"ok": true}
```

---

# 2. Provider simulator (`:9000`)

The simulator acts as the external LLM provider. It enforces its **own** copy of each model's limits over a strict rolling 60 s window, with no 1% guard, and returns 429 when a request would exceed them. It simulates latency and outcomes, and records every accepted request in 10 ms buckets for the independent auditor. The control plane calls it; you normally do not, except to inspect it or inject failures.

## 2.1 Run an inference

`POST /v1/infer`

| Field | Type | Required | Description |
|---|---|---|---|
| `request_id` | string | yes | |
| `model` | string | yes | |
| `estimated_tokens` | integer | yes | |
| `payload` | any | no | |
| `attempt` | integer | no (default 1) | The attempt number. Informational only. |

| Status | Meaning | Body |
|---|---|---|
| `200` | Success, returned after the simulated latency | `{request_id, model, output, usage: {estimated_tokens, actual_tokens}, latency_ms}` |
| `429` | The request would exceed the provider's rolling-60 s limit | `{"error": "rate_limited", "request_id": ...}` |
| `503` | Transient failure (retryable) | `{"error": "transient", "request_id": ...}` |
| `400` | Permanent failure (not retryable) | `{"error": "invalid_request", "request_id": ...}` |
| `404` | Unknown model | |

```bash
curl -s -X POST http://127.0.0.1:9000/v1/infer \
  -H 'Content-Type: application/json' \
  -d '{"request_id": "manual-1", "model": "model-a", "estimated_tokens": 1000, "payload": {"prompt": "hi"}}'
```

```json
{"request_id": "manual-1", "model": "model-a", "output": "simulated completion for manual-1",
 "usage": {"estimated_tokens": 1000, "actual_tokens": 941}, "latency_ms": 163.7}
```

> Requests sent directly to the simulator count against its limits and appear in the audit evidence. To keep scenario results clean, avoid calling it directly while a scenario is running.

## 2.2 List models

`GET /v1/models`

Returns limits, behavior settings and outcome counters for each model.

```bash
curl -s http://127.0.0.1:9000/v1/models
```

```json
{
  "model-a": {
    "rpm": 50000, "tpm": 100000000,
    "behavior": {"latency_ms": 150.0, "jitter_ms": 50.0, "tail_prob": 0.01, "tail_ms": 1000.0,
                 "p_transient": 0.0, "p_permanent": 0.0, "token_noise": [0.8, 1.05]},
    "counters": {"accepted": 20000, "rate_limited": 0, "success": 20000, "transient_error": 0, "permanent_error": 0}
  }
}
```

## 2.3 Change provider limits

`PUT /v1/models/{model}/limits`, with body `{"rpm": int, "tpm": int}`.

The control plane calls this automatically when you change limits with `propagate_to_provider: true`. Call it directly only to make the provider and the dispatcher disagree on purpose, for example to watch the dispatcher handle provider 429s.

```bash
curl -s -X PUT http://127.0.0.1:9000/v1/models/model-a/limits \
  -H 'Content-Type: application/json' -d '{"rpm": 5000, "tpm": 60000000}'
```

```json
{"model": "model-a", "rpm": 5000, "tpm": 60000000}
```

## 2.4 Change simulated behavior (failure injection and latency)

`PUT /v1/models/{model}/behavior`

All fields are optional. Only the fields you send are changed.

| Field | Description |
|---|---|
| `latency_ms` | The base latency. |
| `jitter_ms` | Uniform ± jitter. |
| `tail_prob`, `tail_ms` | The probability of a slow response, and the maximum extra latency for one. |
| `p_transient` | The probability of a 503, which the dispatcher retries. |
| `p_permanent` | The probability of a 400, which becomes `failed`. |
| `token_noise` | `[lo, hi]`. Actual tokens = estimate × U(lo, hi). |

```bash
curl -s -X PUT http://127.0.0.1:9000/v1/models/model-a/behavior \
  -H 'Content-Type: application/json' \
  -d '{"p_transient": 0.05, "p_permanent": 0.02, "latency_ms": 200}'
```

## 2.5 Audit evidence

`GET /v1/audit/{model}`

Returns the raw evidence the auditor uses: accepted requests and metered tokens per 10 ms bucket, plus the provider's limit history.

| Field | Description |
|---|---|
| `bucket_s` | `0.01` |
| `buckets` | Bucket ids. Bucket start time = `id × bucket_s`, in epoch seconds. |
| `counts`, `tokens` | Accepted requests and actual tokens per bucket, aligned with `buckets`. |
| `limit_history` | `[{at, rpm, tpm}]` |
| `counters` | The same as in §2.2. |

```bash
curl -s http://127.0.0.1:9000/v1/audit/model-a
```

To evaluate it, run the auditor from `backend/`. It exits non-zero if any 60 s window exceeds a limit.

```bash
python -m tools.auditor --simulator http://127.0.0.1:9000 --model model-a --model model-b
```

## 2.6 Reset evidence and health

```bash
curl -s -X POST http://127.0.0.1:9000/v1/admin/reset     # clears audit evidence + counters
curl -s http://127.0.0.1:9000/health
```

---

# 3. Mock callback receiver (`:9100`)

A stand-in for a client's webhook. It rejects the first *N* delivery attempts for each batch with `503` to simulate an outage (default *N* = 2), then accepts. It deduplicates on `Idempotency-Key`.

## 3.1 Receive a callback

`POST /callback`

This is the endpoint the control plane posts to. Responses:

- `503 {"error": "simulated outage", "attempt": n}` while `n ≤ reject_first`
- then `200 {"ok": true}`
- `200 {"ok": true, "duplicate": true}` for repeat deliveries of the same batch

```bash
curl -s -X POST http://127.0.0.1:9100/callback \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: b-test1' \
  -d '{"batch_id": "b-test1", "status": "completed", "total": 1, "successful": 1, "failed": 0}'
```

## 3.2 Inspect what was received

`GET /received`

Returns `delivered` (keyed by batch id, with `received_at`, `attempt` and `body`) and `log` (every attempt with its time and whether it was accepted).

```bash
curl -s http://127.0.0.1:9100/received
```

```json
{
  "delivered": {"b-dd08e6384bb8": {"received_at": 1794418021.77, "attempt": 3, "body": {"batch_id": "b-dd08e6384bb8", "...": "..."}}},
  "log": [
    {"batch_id": "b-dd08e6384bb8", "attempt": 1, "at": 1794418018.58, "header_attempt": "1", "accepted": false},
    {"batch_id": "b-dd08e6384bb8", "attempt": 2, "at": 1794418019.67, "header_attempt": "2", "accepted": false},
    {"batch_id": "b-dd08e6384bb8", "attempt": 3, "at": 1794418021.77, "header_attempt": "3", "accepted": true}
  ]
}
```

## 3.3 Change the number of rejected attempts, reset, and health

```bash
curl -s -X PUT "http://127.0.0.1:9100/config?reject_first=0"   # accept immediately from now on
curl -s -X POST http://127.0.0.1:9100/reset                      # forget attempts and deliveries
curl -s http://127.0.0.1:9100/health
```

---

# 4. End-to-end walkthrough (copy and paste in order)

```bash
# 0) start everything (from backend/):  python -m tools.up

# 1) single request -> poll it
RID=$(curl -s -X POST http://127.0.0.1:8000/v1/requests -H 'Content-Type: application/json' \
      -d '{"model":"model-a","estimated_tokens":1000,"payload":{"prompt":"hello"}}' | python -c "import sys,json;print(json.load(sys.stdin)['request_id'])")
sleep 1; curl -s http://127.0.0.1:8000/v1/requests/$RID

# 2) inject provider failures
curl -s -X PUT http://127.0.0.1:9000/v1/models/model-a/behavior -H 'Content-Type: application/json' \
     -d '{"p_transient":0.05,"p_permanent":0.02}'

# 3) batch with a callback (the mock receiver rejects the first 2 attempts)
BID=$(curl -s -X POST http://127.0.0.1:8000/v1/batches -H 'Content-Type: application/json' \
      -d '{"callback_url":"http://127.0.0.1:9100/callback","requests":[{"model":"model-a","estimated_tokens":1000},{"model":"model-b","estimated_tokens":1000}]}' \
      | python -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")
sleep 6
curl -s http://127.0.0.1:8000/v1/batches/$BID            # sealed summary + callback history
curl -s http://127.0.0.1:8000/v1/batches/$BID/results    # every request id exactly once
curl -s http://127.0.0.1:9100/received                   # what the "client" received

# 4) change limits live and watch the window
curl -s -X PUT http://127.0.0.1:8000/v1/models/model-a/limits -H 'Content-Type: application/json' -d '{"rpm":5000,"tpm":60000000}'
curl -s "http://127.0.0.1:8000/v1/metrics/timeseries?model=model-a" | python -c "import sys,json;p=json.load(sys.stdin)['points'];print(p[-1] if p else 'no points yet')"

# 5) totals + accounting invariant
curl -s http://127.0.0.1:8000/v1/metrics
```

In step 2, the first transient failures are retried (up to 4 attempts with 0.2 s → 0.4 s → 0.8 s backoff). In step 3, the callback arrives about 3 s after sealing because of the two simulated rejections (backoff 1 s, then 2 s).
