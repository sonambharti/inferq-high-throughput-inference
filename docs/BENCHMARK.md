# Benchmark report

This report has two parts, kept strictly apart:

- **Measured:** numbers produced by the commands in this repository on the machine described below. The raw data is in `reports/*/result.json`, with per-run reports and charts in `reports/*/report.md`.
- **Projected:** estimates derived from the measured numbers. They are labelled as projections and never mixed into the measured tables.

---

## 1. Test environment

| Item | Value |
|---|---|
| OS | Windows 11 Home (10.0.26200), x86-64 |
| CPU | 8 logical processors (laptop-class) |
| Python | 3.14 (CPython), NumPy 2.5, FastAPI 0.162, aiohttp 3.14, uvicorn |
| Topology | Load generator, control plane, provider simulator and mock receiver all on **one machine**, talking over loopback HTTP |
| Date | 2026-11-11/12 |

Every scenario and benchmark run starts fresh processes. Each validation scenario is a single command (`python -m tools.scenarios s1|s2|s3`), and the benchmark is `python -m tools.bench --all`.

## 2. Simulation assumptions

- **Provider latency:** 150 ms base, ±50 ms uniform jitter, and a 1% tail of up to +1 s. This is an assumption, not a measurement of a real provider.
- **Token accounting:** actual tokens = estimate × U(0.8, 1.05) on success. The simulated provider meters actual tokens. The dispatcher reserves the estimate and reconciles to the actual count.
- **Failure injection:** none in Scenarios 1 and 2. Scenario 3 uses 5% transient failures (retried) and 2% permanent failures. The benchmark uses 1% permanent failures.
- **Latency measurement:** from each request's *intended* send time (open-loop generator), so it includes queueing under overload.
- **Guard band:** the dispatcher keeps 1% below each limit. Requests have a 60 s deadline (120 s in Scenario 3).
- **Two transports:**
  - Scenarios 1–3 run end-to-end over **HTTP**.
  - The 300k/s and 1M/s benchmarks run the same scheduling rules and the same provider outcome model **in-process**, on NumPy frames across worker processes. They do not include HTTP or JSON per request.

---

## 3. Measured: validation scenarios (HTTP transport)

### Scenario 1: reach provider capacity (50,000 RPM / 100M TPM): **PASS**

Offered load: 1,000 req/s of 1,000-token requests (120% of capacity) for 330 s.

| Criterion | Result |
|---|---|
| Completion ≥ 90% of allowed capacity after warm-up | **49,023 completed/min = 98.0%** (lowest full 60 s window: 48,522) |
| No 60 s window exceeds RPM (independent auditor, 10 ms resolution, 27,213 windows) | Peak **49,539** / 50,000 (99.08%), 0 violating windows |
| No 60 s window exceeds TPM | Peak 45.8M / 100M (45.8%), 0 violating windows |
| Every request accounted for | 329,992 acked = 271,397 completed + 47,580 waiting + 501 expired + 10,514 rejected. 0 transport errors. |
| Provider 429s | 0 |
| Latency p50 / p95 / p99 | 29.4 s / 57.2 s / 59.7 s. This is queueing by design: offered load is 120% of capacity, so the excess waits up to its 60 s deadline. Provider latency itself is about 150 ms. |

TPM is not the binding limit here: 1,000 tokens × 50,000 RPM = 50M tokens per minute, which is under 100M. The guard band holds the observed peak just under the limit. The ledger caps at 49,500 per 60.1 s at send time; the provider sees arrivals shifted by network jitter, so its peak is 49,539, still below 50,000.

Charts: [reports/s1/rpm.svg](../reports/s1/rpm.svg). Full report: [reports/s1/report.md](../reports/s1/report.md).

### Scenario 2: two models, changing limits: **PASS**

Model A runs at 30k RPM / 60M TPM and Model B at 20k RPM / 40M TPM. Each is offered 120% of its limit. A is lowered to 5,000 RPM at t=120 s and restored to 30,000 RPM at t=240 s, in a 360 s run.

| Criterion | Result |
|---|---|
| A within its current limits (auditor) | Peak 29,733 / 30,000 (99.1%), 0 violating windows |
| B within its limits (auditor) | Peak 19,812 / 20,000 (99.1%), 0 violating windows |
| A adapts to the reduction, without a restart | **4,932/min** while constrained, after its window drained (limit 5,000). The trailing 60 s window just before the restore was 4,933. |
| A adapts to the restore, without a restart | **29,161/min** after the restore (limit 30,000) |
| B keeps processing while A is constrained | 19,698/min before vs **19,750/min** during A's constraint (100.3%) |
| Limits and throughput over time | [reports/s2/rpm.svg](../reports/s2/rpm.svg), [reports/s2/rate.svg](../reports/s2/rate.svg), and a 15 s timeline table in the report |

What happened at the drop: 12,176 waiting A requests whose new slot fell past their deadline expired immediately. A then sent nothing for about 50 s, until its trailing window fell below 5,000, and after that paced at about 82/s.

### Scenario 3: asynchronous batch completion: **PASS**

10,000 requests across A and B, with 5% transient and 2% permanent failures. The receiver rejects the first 2 callback attempts.

| Criterion | Result |
|---|---|
| Ack within 1 s | **282 ms** for 10,000 requests |
| Callback only after every request is final | Last request final at +16.95 s, sealed at +16.95 s, first callback attempt at +17.09 s |
| Delivery succeeds after recovery | Attempts: 503, 503, **200** (delivered on attempt 3) |
| Callback body equals `GET /v1/batches/{id}` | Identical (same `summary_sha256`) |
| Every request id exactly once | 10,000 results, 10,000 unique ids, all 10,000 client ids present once |
| Outcome | 9,788 succeeded, 212 failed (permanent errors plus transient errors that exhausted retries). 528 requests were retried at least once. |

---

## 4. Measured: throughput benchmarks (in-process transport)

Command: `python -m tools.bench --all --duration 30`. Limits are raised, as the brief permits, to 1.25 × the offered load and split into equal per-worker leases. A request counts only when its simulated provider call reaches a final success or failure. Every worker verifies that each generated request id was finalized **exactly once**, or is still accounted for as waiting or in flight.

| Run | Workers | Offered | **Sustained median** | Lowest 1 s | Completed | p50 / p95 / p99 | Exactly-once / accounted / lease respected |
|---|---|---|---|---|---|---|---|
| Required 300k/s | 8 | 330,000/s | **329,115/s** | 311,614/s | 9.89M | 196 / 269 / 335 ms | ✓ / ✓ / ✓ |
| Stretch 1M/s | 8 | 1,100,000/s | **1,101,677/s** | 1,022,636/s | 32.97M | 254 / 485 / 721 ms | ✓ / ✓ / ✓ |

Both targets are met. Each run offers 10% more than the target, so a pass means "sustained ≥ target" rather than "kept up with exactly the target".

**Scaling curve.** Maximum sustained throughput, generating as fast as possible for 10 s per point:

| Workers | Max sustained | Per worker | Efficiency vs 1 worker |
|---|---|---|---|
| 1 | 1.65M/s | 1.65M/s | 100% |
| 2 | 3.30M/s | 1.65M/s | 100% |
| 4 | **4.89M/s** | 1.22M/s | 74% |
| 8 | 3.70M/s | 0.46M/s | 28% |

Charts: [reports/bench/rate_300k.svg](../reports/bench/rate_300k.svg), [rate_1m.svg](../reports/bench/rate_1m.svg), [scaling.svg](../reports/bench/scaling.svg).

### Measured: HTTP-path ceiling

To size the HTTP path honestly, limits were raised to 10M RPM and a single control-plane process was overdriven. The load generator, control plane and simulator shared the box.

| Offered | Completed/s (median) | Notes |
|---|---|---|
| 2,000 req/s | **1,137/s** | 0 transport errors; the backlog drained after the run |
| 4,000 req/s | 1,051/s | Same ceiling; latency grows with queueing |

---

## 5. Bottlenecks and observations

1. **Per-request HTTP in Python is the dominant cost.** One control-plane process completes about 1.1k req/s end to end. Each request costs an inbound HTTP request plus JSON, an outbound aiohttp call to the provider, and a response. That leaves about 35% headroom over Scenario 1's ~825 req/s, but it is about 300× below the benchmark rate. This is why the high-throughput path is vectorized and in-process.
2. **The engine logic is not the bottleneck.** Admission, the rolling-window ledger, the timing wheel and finalization run at 1.2–1.65M requests/s per worker when frames amortize the Python overhead.
3. **Physical cores limit scaling.** Throughput scales linearly up to 2 workers, reaches 74% efficiency at 4, and drops at 8. The 8 logical processors share fewer physical cores and memory bandwidth, and the OS plus the parent process compete for them. On this machine, 4 workers is the efficient point.
4. **Tail latency at 1M/s.** p99 rises from 335 ms to 721 ms because each worker's 10 ms tick processes larger frames, so finalization trails completion by more ticks. Simulated provider latency is unchanged.
5. **Environment sensitivity.** A laptop that sleeps mid-run invalidates a timed run: wall-clock time jumps and expires queued requests. The runners now ask Windows to stay awake while they run, and every scenario includes a "not stalled" criterion. One earlier run failed because of a suspend; it is not included above.
6. **Overload behavior is visible and bounded.** Under 120% load, queues stop growing once the projected wait reaches the 60 s deadline. From then on the excess is rejected at the door with `retry_after_s` (10,514 in Scenario 1). Expiry is rare (501 in Scenario 1) and happens mainly after limit drops (12,176 in Scenario 2).

---

## 6. Projected: toward 10 billion requests per minute (not measured)

10B/min is about **166.7M completions/s**. Every number in this section is a projection from the measurements above, under the stated assumptions.

**Architecture** (the measured unit, repeated):

- Partition by **model**, and within a model by **request-id range**, into single-writer workers like the benchmark workers.
- Each worker enforces a **capacity lease**, a short-TTL slice of the model's RPM and TPM. Leases come from a per-model quota service arranged in a hierarchy: region → node → worker. The sum of live leases is never more than the provider limit. The lease TTL (for example 1 s) bounds how fast a limit change propagates. Unused lease is returned at renewal.
- Intake is a partitioned log (for example Kafka, partitioned by model and shard). Clients and batch submission append to it, and workers consume their partitions.
- Provider calls go out over pooled HTTP/2 connections.
- No single provider account serves 167M/s. The plan assumes many accounts or deployments, each with its own lease tree.

**Sizing:**

| Layer | Basis | Projection |
|---|---|---|
| Scheduling engine (ledger, admission, finalization) | Measured 1.22M/s per worker at 74% efficiency (4 workers on this laptop) | 166.7M / 1.22M ≈ **137 worker processes**. At 4 efficient workers per 8-thread box, that is about **35 laptop-class nodes**, or about **9 nodes** with 16 physical cores each. |
| Provider I/O (assumed compiled HTTP/2 client, e.g. Go or Rust, at ~50k req/s per core; **assumption, not measured here**) | 166.7M / 50k | about **3,300 cores**, roughly **105 32-core nodes** |
| Provider I/O (measured Python aiohttp path, ~1.1k req/s per process) | 166.7M / 1.1k | about 150,000 processes. Not viable: the I/O tier must not be Python per request. |
| Lease traffic | 1 renewal per worker per second | about 3,300 to 150,000 renewals/s across all quota services. This is negligible next to request traffic, and the hierarchy keeps each quota service small. |

**Conclusion (projected):** the scheduling rules proven here are not the limit at 10B/min; network I/O is. At that scale the provider-facing tier should be a compiled, multiplexed HTTP client fed by the same lease-based ledger. The Python control plane would remain the place for admission, limit control and batch semantics.

---

## 7. Reproducing

```bash
cd backend
python -m pytest -q tests                      # ledger + auditor unit tests
python -m tools.scenarios all                  # S1, S2, S3, about 13 min -> reports/s1..s3
python -m tools.bench --all --duration 30      # 300k, 1M and the scaling curve, about 3 min -> reports/bench
```

Close CPU-heavy applications first. Results vary with core count. The scenarios need about 1.1k req/s of HTTP capacity on one box, and the benchmark needs at least 4 cores to show 1M/s.
