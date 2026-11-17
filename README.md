# inferq: high-throughput inference under provider limits

inferq processes AI inference requests at high rates while respecting each model's provider capacity, given as **requests per minute (RPM)** and **tokens per minute (TPM)**. Limits can change while the system runs.

It replaces a synchronous loop that managed about 600 RPM. The project contains:

| Component | What it does | Where |
|---|---|---|
| **Control plane** (FastAPI) | Accepts single and batch requests. Exposes status, results and metrics, applies live limit changes, and delivers batch callbacks. | [backend/inferq/](backend/inferq/) |
| **Dispatch engine** | Runs a per-model *strict rolling-60s slot ledger*, a scheduler, retries, expiry and request accounting. | [engine.py](backend/inferq/engine.py), [ledger.py](backend/inferq/ledger.py) |
| **Provider simulator** | Configurable models, limits, latency and outcomes. Enforces its own limits and records its own evidence for auditing. | [backend/simulator/](backend/simulator/) |
| **Load generator** | Open-loop: you choose rate, duration, model mix, single or batch mode, batch size and token size. | [tools/loadgen.py](backend/tools/loadgen.py) |
| **Independent auditor** | Recomputes peak rolling-60s usage from the *simulator's* records only. | [tools/auditor.py](backend/tools/auditor.py) |
| **Scenario runner** | Runs one command per validation scenario and evaluates every pass criterion as PASS/FAIL. | [tools/scenarios.py](backend/tools/scenarios.py) |
| **Throughput benchmark** | Multi-process vectorized engine for the 300k/s and 1M/s targets, plus a scaling curve. | [tools/bench.py](backend/tools/bench.py) |
| **Dashboard** (React) | Live throughput vs limits, live limit editor, batches and callbacks, accounting. | [frontend/](frontend/) |

The requirements and design rationale are in [docs/prd.md](docs/prd.md). Measured results are in [docs/BENCHMARK.md](docs/BENCHMARK.md) and [reports/](reports/).

---

## 1. Quick start

**Requirements:** Python 3.11 or newer (developed on 3.14), Node 18 or newer (for the dashboard only). Developed on Windows 11. The commands below use bash syntax. On PowerShell, use `.venv\Scripts\python`.

```bash
# from the repository root
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt      # Linux/macOS: .venv/bin/python
cd frontend && npm install && cd ..                                   # dashboard (optional)
```

Start everything: simulator on :9000, control plane on :8000, mock callback receiver on :9100, and the dashboard on :5173.

```bash
cd backend
../.venv/Scripts/python -m tools.up --dashboard
```

Open http://127.0.0.1:5173 for the dashboard and http://127.0.0.1:8000/docs for the interactive API docs.

Or start each piece by hand (from `backend/`):

```bash
python -m simulator.server --port 9000                         # provider simulator
python -m inferq.api --port 8000 --provider-url http://127.0.0.1:9000
python -m tools.mock_receiver --port 9100 --reject-first 2     # optional webhook target
cd ../frontend && npm run dev                                  # dashboard
```

Send some load:

```bash
python -m tools.loadgen --rate 1000 --duration 60 --models model-a=0.6,model-b=0.4 --tokens 1000
```

Run the tests:

```bash
python -m pytest -q tests
```

---

## 2. Running the validation scenarios

Each scenario starts its own simulator, control plane and receiver on ports 9000/8000/9100, so stop `tools.up` first. Each one drives load, audits the simulator's evidence, prints one **PASS/FAIL line per criterion**, and writes `reports/<scenario>/report.md`, `result.json` and SVG charts. The exit code is non-zero if any criterion fails.

```bash
cd backend
python -m tools.scenarios s1      # ~6 min
python -m tools.scenarios s2      # ~6 min
python -m tools.scenarios s3      # ~30 s
python -m tools.scenarios all
# --quick shortens s1/s2 for development. A quick run is marked FAIL on the duration criterion on purpose.
```

| Scenario | Setup | Pass criteria (checked automatically) |
|---|---|---|
| **S1: reach provider capacity** | `model-a` at 50,000 RPM / 100M TPM. 1,000 req/s of 1,000-token requests (120% of capacity) for 330 s. | ≥90% of allowed capacity completed after a 30 s warm-up. No 60 s window over RPM or TPM (auditor, 10 ms resolution). Every submitted request accounted for as completed, waiting, expired or rejected. |
| **S2: two models, changing limits** | A: 30k RPM / 60M TPM, B: 20k RPM / 40M TPM, both offered 120%. A is lowered to 5,000 RPM at t=120 s and restored at t=240 s, in a 360 s run. | Each model within its current limits (auditor). A adapts to both changes without a restart. B keeps ≥95% of its pre-change throughput. The report shows limits and throughput over time. |
| **S3: async batch** | 10,000 requests across A and B. 5% transient and 2% permanent provider failures. The receiver rejects the first 2 callback attempts. | Ack in under 1 s. Callback only after every request is final. Delivery succeeds after recovery. Callback body equals `GET /v1/batches/{id}`. Every request id appears exactly once in the results. |

Throughput benchmarks (in-process transport, see §5):

```bash
python -m tools.bench --target 300000 --duration 30     # required: 300k completed/s
python -m tools.bench --target 1000000 --duration 30    # stretch: 1M completed/s
python -m tools.bench --all                             # both + 1/2/4/8-worker scaling curve -> reports/bench/
```

---

## 3. Configuring models and changing limits

Models are defined in [backend/config/models.yaml](backend/config/models.yaml). The control plane and the simulator both read it. Pass `--config path.yaml` or set `INFERQ_CONFIG` to use another file.

```yaml
defaults:
  deadline_s: 60          # no send slot before this -> rejected at admission, or expired while waiting
  max_attempts: 4         # provider attempts per request (transient errors and 429s are retried)
  guard_fraction: 0.01    # 1% headroom below each limit
models:
  model-a: { rpm: 50000, tpm: 100000000 }
  model-b: { rpm: 20000, tpm: 40000000 }
simulator:
  default: { latency_ms: 150, jitter_ms: 50, tail_prob: 0.01, tail_ms: 1000,
             p_transient: 0.0, p_permanent: 0.0, token_noise: [0.8, 1.05] }
```

Change limits **while the system is running**, with no restart:

```bash
curl -X PUT http://127.0.0.1:8000/v1/models/model-a/limits \
     -H 'content-type: application/json' -d '{"rpm": 5000, "tpm": 60000000}'
```

You can also use the "Apply limits live" form on the dashboard. A new model name creates that model on the fly. By default the change is also sent to the simulator (`propagate_to_provider: true`), because in this simulation the operator stands in for the provider changing its limits. Simulator behavior such as failure rates and latency can also be changed live with `PUT :9000/v1/models/{model}/behavior`.

---

## 4. API reference (control plane, :8000)

**Full API documentation, with every field, status code and a copy-paste `curl` for each endpoint (importable into Postman): [backend/api_doc.md](backend/api_doc.md).**

| Method & path | Purpose |
|---|---|
| `POST /v1/requests` | Submit one request: `{model, estimated_tokens, payload?, request_id?, deadline_s?, intended_at?}`. Returns **202** `{request_id, state: "waiting", eta_s}`, or **429** `{state: "rejected", reason, retry_after_s}` with a `Retry-After` header. |
| `GET /v1/requests/{id}` | State, attempts, tokens (estimated and actual), output or error, timestamps, latency. |
| `POST /v1/batches` | `{requests: [...up to 100k], callback_url?, deadline_s?}`. Returns **202** `{batch_id, status_url, results_url}` immediately. |
| `GET /v1/batches/{id}` | Batch status. Once sealed, this is the *same immutable summary* that the callback sends, plus a `callback` delivery block with the full attempt history. |
| `GET /v1/batches/{id}/results?offset=&limit=` | Paged per-request results, once the batch is sealed. |
| `GET /v1/batches` | Recent batches. |
| `GET /v1/models` · `PUT /v1/models/{m}/limits` | Read or change limits live. |
| `GET /v1/metrics` | Per-model and total state counts, the accounting invariant, trailing-60s usage, and latency percentiles. |
| `GET /v1/metrics/timeseries?since=` | Per-second series per model: limits, window usage, sent/completed/expired/rejected per second. |
| `POST /v1/admin/reset` | Clear counters and history for a fresh run. |

**Request lifecycle:** `waiting → in_flight → succeeded | failed` (*completed*), `waiting → expired`, or `rejected` at admission.
The invariant `submitted = waiting + in_flight + succeeded + failed + expired + rejected` is checked continuously and shown on the dashboard.

**Callback** (`POST callback_url`) has headers `Idempotency-Key: <batch_id>`, `X-Inferq-Batch-Id` and `X-Inferq-Attempt`. The body looks like this:

```json
{"batch_id": "b-…", "status": "completed_with_errors", "total": 10000, "successful": 9788, "failed": 212,
 "counts": {"succeeded": 9788, "failed": 212, "expired": 0, "rejected": 0},
 "created_at": …, "sealed_at": …, "duration_s": 16.9,
 "results_url": "http://127.0.0.1:8000/v1/batches/b-…/results", "summary_sha256": "…"}
```

---

## 5. Key design decisions and tradeoffs

1. **Strict rolling window, not a token bucket.** Each model's ledger holds two rings of 601 × 100 ms buckets, one for requests and one for tokens. A request is sent only if the trailing 60.1 s sum plus its cost stays within `limit × (1 − guard)`. 601 buckets (not 600) cover every real 60-second interval whatever its sub-bucket offset. A token bucket with a burst can legally emit burst plus 60 s of refill inside one window, and a fixed window can emit 2× the limit across a boundary. Either would fail "no observed 60-second period exceeds either limit".
2. **Pacing.** At most `limit / 600` sends per 100 ms bucket. Without it, a fresh window is spent in the first seconds, which produces a 50 s on / 10 s off sawtooth. With it, throughput is a flat line at about 99% of the limit.
3. **Booking a slot is the overload policy.** On admission the engine projects when the request could be sent. If that is after the request's deadline (60 s by default), it is **rejected immediately** with `retry_after_s` instead of joining an unbounded queue. Waiting requests expire when a limit drop pushes their slot past their deadline, and they are re-projected right away. Queues therefore stay bounded by about `rate × deadline`, and each of the brief's four states has one clear cause.
4. **Reserve, then reconcile tokens.** TPM is charged from `estimated_tokens` at send time and corrected to the provider-reported actual count at completion. If actual is lower, the difference is **refunded to the original bucket** while it is still inside the window, and dropped once that bucket has expired. If actual is higher, the extra is **charged to the current bucket**. Refunding into the current bucket would understate trailing usage and allow overshoot.
5. **Limit drops carry debt.** Usage already sent in the trailing 60 s still counts against a lowered limit, so the model pauses until the window drains, then paces at the new rate. The auditor checks each window against the *highest* limit in force during that window, because a lowered limit cannot un-send requests made under the old one.
6. **Independent audit.** The simulator acts as the provider and records accepted arrivals and metered tokens in 10 ms buckets. The auditor computes peak rolling-60s usage from those records only and never imports the dispatcher's limiter. A limiter that grades itself is not evidence.
7. **Sealed batch statement.** A batch's remaining count reaches zero on exactly one final transition. That transition seals the batch: the immutable summary, the results and the outbox row are written in one SQLite transaction. `GET` and the callback serve the same sealed summary, so they cannot disagree. The callback is delivered at-least-once with exponential backoff (1 s base, doubling, ±20% jitter, 12 attempts) and an idempotency key. It survives restarts because the outbox is durable.
8. **Two transports, labelled.** Per-request HTTP and JSON in Python manages a few thousand requests per second per process. That is plenty for the 50k-RPM scenarios, which run over real HTTP, but far below 300k/s. The throughput benchmark therefore runs the *same* ledger rules and the *same* simulator outcome model **in-process**, on NumPy frames of requests across worker processes. Simulated latency is held in a timing wheel. Every number in every report states which transport produced it.
9. **Capacity leases.** In the benchmark each worker process owns an equal share of every limit, so the sum of the shares never exceeds the limit and no cross-process coordination is needed. The same mechanism is how the design scales out to many nodes (§6).
10. **What is durable.** Batches, sealed summaries, results and the callback outbox are stored in SQLite (WAL). Hot-path request state is held in memory, because a database write per request would cap throughput. Consequence: a batch that is still processing when the service restarts is marked `interrupted`. Callbacks for batches already sealed are still delivered after the restart.

**Out of scope:** real LLM calls; discovering limits from 429s (limits come from configuration and the operator); HMAC-signed webhooks, dead-letter queues and manual replay; authentication; separate input and output TPM; daily limits; and multi-node deployment, which is a projection only.

---

## 6. Toward 10 billion requests per minute (projection, not measured)

10B/min is about **167M completions/s**. The design scales by repeating the measured unit horizontally:

1. **Shard by model, then by request-id range within a model.** Each shard is a single-writer worker like the benchmark workers, with no locks and no shared counters on the hot path.
2. **Hierarchical capacity leases (Doorman pattern).** A per-model quota service leases short-TTL slices of each provider limit to regions, regions lease to nodes, and nodes lease to workers. Enforcement is always local, and the sum of live leases never exceeds the limit. The lease TTL (for example 1 s) bounds how quickly a limit drop takes effect. Unused leases are returned.
3. **Log-based intake.** Clients write to a partitioned log (for example Kafka, partitioned by model and shard). Dispatch workers consume their partitions, so intake bursts never block dispatch.
4. **Provider fan-out.** No single provider account serves 167M/s. The plan assumes many accounts or deployments, each with its own ledger and lease tree.
5. **Sizing.** `nodes = 167M / (measured per-worker rate × workers per node × scaling efficiency)`. The measured per-worker rate and the scaling efficiency come from `tools.bench --all`. The worked numbers are in [docs/BENCHMARK.md](docs/BENCHMARK.md) under *Projected*.

---

## 7. Repository layout

```
backend/
  config/models.yaml        model limits + simulator behaviour
  inferq/                   control plane: api.py, engine.py, ledger.py, batches.py, config.py
  simulator/                provider simulator: server.py (HTTP), core.py (shared outcome model)
  tools/                    loadgen, auditor, scenarios, bench, mock_receiver, charts, up
  tests/                    ledger + auditor unit tests
frontend/                   React + Vite dashboard
docs/
  high-throughput-inference-assignment.pdf   the brief
  prd.md                    product requirements + design notes
  BENCHMARK.md              benchmark report (measured vs projected)
reports/                    generated scenario/benchmark reports (report.md, result.json, *.svg)
```

## 8. Simulation assumptions (stated once, repeated in every report)

- **Latency.** Simulated provider latency is 150 ms ± 50 ms (uniform), with a 1% tail of up to +1 s. These are assumptions, not measurements of a real provider.
- **Tokens.** Actual tokens are the estimate × U(0.8, 1.05) on success. The provider meters actual tokens.
- **Network.** Every scenario runs on one machine over loopback HTTP. Network distance to a real provider is represented only by the simulated latency.
- **Latency measurement.** Latency is measured from each request's *intended* send time (open-loop load), so it includes queueing under overload.
- **Benchmark transport.** The benchmark numbers come from the in-process transport, not end-to-end HTTP. The 50k-RPM scenarios use real HTTP.

## 9. How AI was used

This project was built with Claude Code. AI was used for:

- **Ideation:** five parallel ideation agents explored the problem, a separate verifier checked their claims, and web research covered prior art on RPM/TPM accounting, Doorman leases and webhook retry conventions.
- **Writing the PRD.**
- **Code generation.**

Human-owned decisions: the choice of Python and React; the scope cuts (no limit inference from 429s, no HMAC or dead-letter queue); and checking the subtle correctness rules, namely refund direction, the 601-bucket window, limit-drop semantics and the independent auditor, against the brief's pass criteria.
