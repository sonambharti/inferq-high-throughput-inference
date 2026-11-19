# Interview prep: inferq, a high-throughput inference dispatcher

How to use this document: read sections 1–4 before the interview, keep section 12 (numbers) and section 13 (Q&A) open during it, and use section 14 as the live-demo runbook. File references are relative to `backend/`.

**Round 3 agenda (Ankur Datta):**

1. **Break down what we built.** Use sections 1–9 and 11.
2. **Restructure to build specific parts, and rethink the way we built it.** Use **section 16**.
3. **Personal project deep dive.** Use **section 17** as the frame.

---

## 1. The problem in one minute

- **Today:** a loop does `requests.post(...)` followed by `sleep(0.1)`. That is about 600 requests per minute against a provider that allows **50,000 RPM and 100M TPM**, so roughly 1% of paid capacity is used.
- **Why "just go faster" isn't enough:** the brief adds six hard constraints.
  1. Limits are **per model** and **change while traffic flows**.
  2. **No 60-second period may ever exceed a limit.** This means a *rolling* window, not "per calendar minute".
  3. **Every request must be accounted for:** completed, waiting, expired or rejected.
  4. **Asynchronous batches with a `callback_url`** that fires only after every item is final, and retries if delivery fails.
  5. **Throughput:** 300k simulated completions/s required, 1M/s stretch, and a plan for 10B/min.
  6. **Evidence a reviewer can rerun,** with measurements kept apart from projections.

**One-sentence pitch:**

> "Each model has a strict rolling-60-second ledger that decides *when* each request may go out. That same ledger defines what happens under overload. Everything is proven by an auditor that reads only the provider's own records, never the limiter's."

---

## 2. Architecture

```
                   ┌──────────────────────── control plane (1 process, 1 asyncio event loop) ────────────────────────┐
 load generator    │                                                                                                  │
 (open-loop) ─────▶│  FastAPI routes ──▶ Engine.submit()  ── admission: ETA vs deadline ──▶ rejected (429 + Retry-After)│
 dashboard  ──────▶│      │                    │                                                                       │
 curl / Postman ──▶│      │                    ▼ waiting                                                               │
                   │      │        ┌── per-model ModelRuntime ──────────────┐   (one scheduler coroutine per model)   │
                   │      │        │ FIFO queue + retry queue               │                                          │
                   │      │        │ SlotLedger: 2 rings × 601 × 100 ms      │── can_send()? ── record() ──┐            │
                   │      │        └────────────────────────────────────────┘                             │            │
                   │      │                                                                                ▼            │
                   │      │                                              aiohttp pool (≤2000 conns) ──▶ POST /v1/infer ─┼──▶ provider simulator
                   │      │                                                                                │            │    (own rolling limits,
                   │      │       final state ◀── reconcile tokens ◀── 200 / 400 / 503 / 429 / net error ◀─┘            │     latency, outcomes,
                   │      ▼                                                                                             │     10 ms arrival evidence)
                   │  BatchManager: remaining-- ─▶ 0 ⇒ SEAL (1 SQLite txn: results + summary + outbox)              │            ▲
                   │                                   └──▶ outbox deliverer coroutine ──▶ POST callback_url ───────────┼─▶ mock receiver
                   │  sampler coroutine (1 s) ─▶ time series ─▶ /v1/metrics, dashboard                                │  (rejects first N)
                   └──────────────────────────────────────────────────────────────────────────────────────────────────┘
 auditor (CLI) ── GET /v1/audit/{model} on the simulator ──▶ peak rolling-60 s per model vs limit history ──▶ PASS/FAIL
 benchmark (CLI) ── N worker processes, each: NumPy frames + same ledger rules + same outcome model + timing wheel
```

| Component | File | Responsibility |
|---|---|---|
| Control plane | `inferq/api.py` | HTTP API, wiring, lifespan start and stop |
| Engine | `inferq/engine.py` | Request state table, admission, per-model schedulers, provider calls, retries, expiry, metrics |
| Ledger | `inferq/ledger.py` | Strict rolling-60 s window, pacing, reservation and reconciliation, limit epochs |
| Batches | `inferq/batches.py` | Batch tracking, sealing, SQLite persistence, callback outbox and delivery |
| Simulator | `simulator/server.py`, `simulator/core.py` | The fake provider: its own limit enforcement, latency and outcomes, audit evidence |
| Load generator | `tools/loadgen.py` | Open-loop traffic, single or batch mode |
| Auditor | `tools/auditor.py` | Independent compliance check from simulator records |
| Scenarios | `tools/scenarios.py` | One command per validation scenario, with PASS/FAIL per criterion |
| Benchmark | `tools/bench.py` | Multi-process vectorized engine for 300k and 1M/s, plus the scaling curve |
| Dashboard | `frontend/src/` | React and Recharts: live charts, limit editor, batches, accounting |

**Why this shape:**

- **Control plane vs. provider:** the brief says "build the system *and* a provider simulation". Running the simulator as a separate service makes the network hop real.
- **Auditor as a third party:** a limiter that grades itself is not evidence.
- **Benchmark as a separate engine:** per-request HTTP in Python cannot reach 300k/s, so the benchmark runs the *same rules* in a form fast enough to measure.

---

## 3. Request lifecycle (follow one request)

States: `waiting → in_flight → succeeded | failed` (together these are *completed*), `waiting → expired`, or `rejected` at admission.

1. **Arrive.** `POST /v1/requests` reaches `Engine.submit()` (`engine.py`). A `Request` object is created (the class uses `__slots__` for memory), `submitted` is incremented, and the absolute deadline is set to `now + deadline_s` (default 60 s).
2. **Admission: book a slot or refuse.** `estimate_wait()` computes:
   - `wait = ledger.seconds_until_fits(1, tokens) + max(ahead / rps, (queued_tokens + tokens) / tps)`
   - The first term is the time until the current window has room. The longer it is, the more "debt" the window carries after a limit drop.
   - The second term is the queue ahead of this request, drained at the effective rate.
   - If `now + wait > deadline`, the request becomes **rejected** immediately: HTTP 429, a `Retry-After` header, and `retry_after_s` in the body.
   - Otherwise it becomes **waiting**, with `eta = now + wait`, and is appended to the model's FIFO.
3. **Dispatch.** The model's scheduler coroutine wakes every 5 ms (`TICK_S`). It first serves **retries** whose backoff has elapsed, then the **FIFO head**:
   - If the head has passed its deadline, it becomes **expired**.
   - If `ledger.can_send(1, est_tokens)` is false, the scheduler stops for this tick. Head-of-line blocking is intentional, because it keeps FIFO fairness.
   - Otherwise `_send()` calls `ledger.record()` to charge the request count and **estimated** tokens to the current 100 ms bucket and remembers the bucket id. The state becomes `in_flight`, and an asyncio task makes the provider call.
4. **Provider answers** (`_call_provider`):
   - **200:** reconcile tokens to `usage.actual_tokens`; the request is **succeeded**.
   - **400:** **failed** (permanent).
   - **503, 429 or a network error:** transient. If attempts < 4 and `now + backoff < deadline`, the request goes back to `waiting` in the retry queue with `not_before = now + backoff`, where backoff = 0.2 s · 2^(n−1) · U(0.8, 1.2), capped at 5 s. Otherwise it is **failed** with reason `attempts_exhausted` or `deadline_passed_during_retry`.
   - **Every attempt is charged to the ledger.** Retries consume capacity, just as they do at a real provider.
5. **Finalize.** `_finalize()` decrements the old state's counter and increments the new one, records latency (from the client's `intended_at` to now), and if the request belongs to a batch, calls `BatchManager.on_final(req)`.

**The invariant always holds:** `submitted = waiting + in_flight + succeeded + failed + expired + rejected`. Every transition goes through one place, `_finalize` or `_send`, which moves exactly one unit from one counter to another.

---

## 4. The ledger: the heart of the design (`inferq/ledger.py`)

### 4.1 Data structure
- Two Python lists of length **601**: `_req[i]` (request count) and `_tok[i]` (tokens), one slot per 100 ms bucket.
- Running sums `_req_sum` and `_tok_sum` over the trailing 601 buckets.
- `_head` is the absolute bucket id (`int(now / 0.1)`); the ring index is `bucket % 601`.
- `_advance()` moves the head forward one bucket at a time. Each step subtracts the bucket that just fell out of the window from the sums and zeroes it. That is O(1) amortized, and O(601) in the worst case after an idle gap. If the gap is longer than the whole window, the ring is simply reset.

### 4.2 Admission check (`can_send`)
A request may be sent only if **all** of these hold:
1. `_req_sum + 1 ≤ eff_rpm` and `_tok_sum + tokens ≤ eff_tpm`, where `eff = floor(limit × 0.99)`. This is the **1% guard**.
2. **Pacing:** the current bucket holds fewer than `cap(b) = floor((b+1)·eff/600) − floor(b·eff/600)`. This is *exact fractional pacing*. With eff = 49,500 the caps alternate between 82 and 83, giving exactly 825/s and 49,500 per 600 buckets with no rounding drift.
   - There is a token pacing cap too. A single large request is allowed into an empty bucket, so it can never be blocked forever.

### 4.3 Why 601 buckets, not 600
A real 60-second interval [t, t+60) that starts partway through a 100 ms bucket touches **601** buckets. Summing 601 buckets is therefore a conservative bound on *every* possible 60 s window, whatever its alignment. A 600-bucket check could miss up to 100 ms of sends.

### 4.4 Why not a token bucket or a fixed window (they will ask)
- **Fixed window** (counter reset every minute): you can send 50k at 0:59.9 and another 50k at 1:00.1, which is 100k inside one real minute. That is 2× the limit across the boundary.
- **Token bucket with capacity B and refill r = limit/60:** inside any 60 s you can emit up to B + 60·r = B + limit. Any burst capacity B > 0 breaks "no 60 s period exceeds the limit". A bucket with B ≈ 0 is just pacing, which the ring already does, and the ring additionally *proves* the window sum.
- **Cloudflare-style sliding-window counter:** an approximation (it weights the previous window), so it can undercount. Fine for abuse protection, not for a hard guarantee.
- **Sliding log:** exact, but O(requests in window) memory, about 50k entries per model. The 601-bucket ring is O(1) memory with 100 ms granularity, and the guard band absorbs that granularity.

### 4.5 Why pacing at all
Without pacing, a fresh window lets you send the whole minute's quota in the first seconds. Then the window is full and you send nothing until those sends age out, a **~50 s on / ~10 s off sawtooth** (worked out during design). The average is fine, but latency and the provider's view are terrible. Pacing gives a flat line at about 99% of the limit. That flat line is what the dashboard shows.

### 4.6 Token reservation and reconciliation (subtle; interviewers love this)
- At send time we charge the **estimate** (`record(1, est)`), because the true count isn't known yet. Providers do the same: OpenAI reserves `max_tokens`.
- When the response arrives with `actual_tokens`, `reconcile(bucket, est, actual)` runs:
  - **actual < est:** refund the difference to the **original bucket**, if that bucket is still inside the window. If it has already aged out, **drop** the refund.
  - **actual > est:** charge the excess to the **current** bucket.
- **Why not refund into the current bucket?** Suppose a request was sent 59.9 s ago with a 1,000-token reservation and used 600. If you subtract 400 from the *current* bucket, then 0.1 s later the original bucket (holding 1,000) leaves the window. The window sum is now 400 lower than the provider's true view for the next 60 s, and you can overshoot. Refunding into the original bucket keeps the ring equal to "what the provider actually metered, at the time it metered it".
- **The worst case is safe.** Overestimates only cost utilization. Underestimates (up to +5% in the simulation) are charged late, by about 150 ms of latency. The 1% guard covers that.

### 4.7 Limit changes (epochs and "debt carry")
- `set_limits()` swaps `eff_rpm` and `eff_tpm` atomically and appends a `LimitEpoch` to the history.
- **Trailing usage keeps counting.** After 30k → 5k, the window may still hold about 29.5k, so headroom is −24.5k. Nothing goes out until enough buckets age out, which takes about 50 s, and then it paces at about 82/s.
- `Engine._reproject()` recomputes the ETA of every waiting request under the new limits. Those whose new slot is past their deadline are **expired immediately** (reason `limit_reduced_slot_past_deadline`) rather than rotting in the queue.
- **Raising a limit** takes effect on the next scheduler tick, within 5 ms.
- **Audit semantics:** a window that spans a drop is checked against the *highest* limit in force during that window. You cannot retroactively un-send what was legal when it was sent. The README states this rule explicitly.

---

## 5. Concurrency and threading model (be precise here)

### 5.1 Control plane: one process, one thread, one asyncio event loop
- Uvicorn runs a single event loop. FastAPI routes, the **per-model scheduler coroutines**, the **provider-call tasks**, the **sampler**, and the **callback deliverer** all run as coroutines **on the same thread**.
- **Consequence: no locks around the ledger or the state table.** Mutations happen between `await` points, so they are atomic with respect to each other: cooperative multitasking. This removes a whole class of races. For example, LiteLLM's router has a check-then-increment race because its counters live in Redis.
- `can_send()` followed by `record()` happens with **no `await` in between**, so the check-and-charge pair is atomic by construction.
- **Concurrency comes from I/O, not threads.** Up to 2,000 provider calls are in flight at once (the aiohttp `TCPConnector(limit=2000)`). By Little's law, 825 req/s × ~0.15 s latency ≈ 125 in flight, so the pool has 16× headroom.
- **Scheduler loop:**
  - It sleeps `TICK_S = 5 ms` between passes while there is work. On Windows, the actual sleep granularity is about 1–15 ms; that's fine because sends are bounded per 100 ms bucket anyway.
  - When the model is idle (both queues empty), it waits on an `asyncio.Event` with a 50 ms timeout instead of spinning. `set_limits` and the retry path call `wake.set()`.
- **Where the event loop can block** (be honest if asked):
  - SQLite writes run synchronously on the loop thread.
  - Sealing a 10k-item batch writes 10k rows in **one transaction**, which takes tens of milliseconds.
  - Originally it was 10k autocommit transactions, which stalled the loop for ~4.7 s and delayed the callback. That was found and fixed during testing (section 11).
  - A production version would move the writes to a thread or use `aiosqlite`.
- `threading.Lock` in `BatchManager._sql()` is defensive. All callers are on the loop thread today, but the lock makes it safe if writes are moved to a thread pool later.
- **The GIL is irrelevant here.** There is only one thread. The limit on this process is single-core CPU: Python HTTP and JSON handling, about 1.1k completed req/s end to end (measured).

### 5.2 Load generator: one process, one event loop, open-loop
- **Each tick it computes how many requests *should* have been sent by now** (`rate × elapsed`, supporting a piecewise rate schedule) and creates a task for each one owed. **It never waits for responses before sending more.** That is "open-loop".
- Each request carries `intended_at`, its scheduled send time. **Latency is measured from that time**, so if the system queues, the delay shows up in p95/p99. This avoids **coordinated omission** (Gil Tene's term): a closed-loop generator slows down when the server slows, hiding the very latency you want to measure.
- **Connections:** a pool of 256 keep-alive connections. Connect-phase failures (`ClientConnectorError`) are retried up to 5 times. That is safe because the request never reached the server, so it cannot be double-submitted. Any other failure is counted as an error and reported.

### 5.3 Benchmark: multi-process, shared-nothing (`tools/bench.py`)
- `multiprocessing` with the **spawn** context. That is the Windows default, and it is explicit so the behavior is the same on Linux.
- N worker processes, each a **single-threaded loop** with **its own capacity lease**: `limit / N` for both RPM and TPM.
  - The leases sum to the limit, so the **global limit holds with zero cross-process coordination**. No shared memory, no locks, no IPC on the hot path.
  - This is the Doorman pattern in miniature.
- All workers start at a common `start_at` timestamp, so their per-second counters line up. At the end each sends one summary dict over an `mp.Queue`.
- **Why processes, not threads:** CPython's GIL serializes Python bytecode, so threads would not scale CPU-bound work. NumPy releases the GIL inside some kernels, but the per-tick Python glue would still serialize.

### 5.4 Simulator: one process, one event loop
- Each request does its admission check, then `await asyncio.sleep(latency)`. Thousands of concurrent "requests in flight" are just sleeping coroutines, which are cheap.
- Its limiter is deliberately a **different implementation** from the dispatcher's (a deque of `[bucket, count, tokens]`, 600 × 100 ms), so a shared bug cannot hide itself.

### 5.5 Dashboard
- The browser polls `/v1/metrics` and `/v1/metrics/timeseries?since=<last t>` every 1 s, incrementally, and keeps 10 minutes client-side. CORS is open (`*`) because this is a local demo.

---

## 6. Load and overload handling (the "what happens at 120%" answer)

- **Capacity:** 50,000 RPM = 833/s nominal. The guard band leaves 49,500/min = 825/s effective.
- **Offered:** 1,000/s. The extra 175/s accumulates in the FIFO.
- **The queue grows until the projected wait reaches the deadline.** 60 s × 825/s ≈ 49.5k waiting. From then on, new arrivals are **rejected at the door** with a retry-after. In Scenario 1 that happened from about t=283 s: 10,514 rejections.
- **Memory and latency are bounded by construction:** queue ≤ about `rate × deadline`, and latency ≤ about the deadline. No unbounded queue, no out-of-memory.
- **Why reject at the door rather than accept and later drop?**
  - The client learns immediately and can back off, or route to another model.
  - "Expired" is reserved for the rare case where something changed after admission, such as a limit drop. That keeps each state's cause unambiguous.
- **The ETA is a projection, not a promise.** Correctness never depends on it; the send-time `can_send()` check is the only thing that enforces limits. An optimistic ETA just turns into an expiry instead of a rejection.
- **Retries consume capacity**, and they are served before new requests so retried work isn't starved.
- **Overload on the client side:** the uvicorn listen backlog was raised to 8192 after a connection-refused burst was observed under load (section 11).

---

## 7. Batches and callbacks (`inferq/batches.py`)

1. `POST /v1/batches` writes a `batches` row (status `processing`), creates a `LiveBatch` with `remaining = total`, and **returns immediately**. Ack latency: 106–282 ms for 10k items, most of it parsing the 10k-item JSON body.
2. Admission happens in a **background task** that yields every 2,000 items (`await asyncio.sleep(0)`), so other requests keep being served.
   - **Subtle ordering fix:** the request id is generated and appended to the batch **before** `engine.submit()`. A request can be rejected synchronously inside `submit`, and if it's the last one, it seals the batch. Appending afterwards would seal without that id.
3. Every final transition calls `on_final`, which does `remaining -= 1` and updates the per-state counts. **The transition that reaches 0 seals the batch, exactly once**, guarded by `b.sealed`.
4. **Sealing is a single SQLite transaction** containing:
   - all result rows (`batch_results`),
   - the immutable summary JSON plus its SHA-256 (`batches.summary`),
   - the outbox row (`outbox`, status `pending`).
   A crash therefore can't leave a sealed batch without its callback, or a callback without results.
5. **One source of truth:**
   - `GET /v1/batches/{id}` serves the sealed summary JSON, plus a separate `callback` block with the delivery state.
   - The callback body is the **same bytes**.
   - Scenario 3 checks that they are identical, field for field.
6. **Outbox deliverer coroutine:**
   - It polls due rows, waking early on an `asyncio.Event` when something is sealed.
   - It POSTs with `Idempotency-Key: <batch_id>`, `X-Inferq-Batch-Id` and `X-Inferq-Attempt`.
   - **Success:** any 2xx response. A timeout (10 s), another status code or a connection error counts as a failure.
   - **Backoff:** 1 s · 2^(n−1) · U(0.8, 1.2), capped at 60 s, for up to 12 attempts, then `failed`.
   - Every attempt is recorded (time, status, error, elapsed) and shown in the API and on the dashboard.
7. **At-least-once delivery.** The receiver deduplicates on the idempotency key; the mock receiver demonstrates this. Exactly-once delivery over HTTP is impossible in general. The standard answer is at-least-once delivery plus idempotent receivers, which is what Stripe and Svix do.
8. **Restart behavior:**
   - Pending outbox rows are durable and are delivered after a restart.
   - Batches still `processing` at shutdown are marked `interrupted`, because request-level state lives in memory.

---

## 8. Provider simulator and auditor (the proof)

### Simulator (`simulator/server.py`)
- For each request it samples the outcome, latency and actual tokens (`core.sample_one`).
- It enforces **its own** rolling-60 s limit on **actual** tokens, returning 429 if exceeded. Zero 429s in every scenario means the dispatcher never relied on the provider to push back.
- It records accepted arrivals in **10 ms buckets**, holding a count and actual tokens.
- Limits and behavior (failure rates, latency) can be changed live. Limit changes are recorded in `limit_history`.

### Auditor (`tools/auditor.py`)
- It fetches `/v1/audit/{model}` and builds dense per-10 ms arrays.
- **Rolling sums:** a cumulative sum, then `c[w:] − c[:-w]` with w = 6,000 buckets (60 s). This is O(n) and checks every 10 ms offset. Scenario 1 checked 27,213 windows.
- **Limit in force per window:** a step function of the limit history, then a **sliding maximum** over 6,000 buckets using the van Herk / Gil-Werman block algorithm, which is O(n). This implements "check against the highest limit in force during the window".
- It reports the peak ratio, the number of violating windows, and a per-second series used for the charts. It exits non-zero on any violation.
- It has **its own unit tests**, including one that deliberately violates the limit to prove it fails when it should.

---

## 9. Throughput benchmark internals (`tools/bench.py`)

### Per-worker loop, each 10 ms tick
1. **Generate (open-loop).** `due = rate × elapsed`. The new ids are a contiguous `arange`, and estimated tokens and arrival times are NumPy arrays appended to the pending arrays.
2. **Advance the ring.** These are the same 601 × 100 ms rules as the real ledger, held in NumPy `int64` arrays.
3. **Expire.** Pending arrivals are time-sorted, so `searchsorted(arrival, now − deadline)` gives the count to expire.
4. **Admit a prefix in O(k).**
   - `room = min(window headroom, pacing cap for this bucket)`.
   - `k = searchsorted(cumsum(est[:room_req]), room_tok, side='right')`, so a single vectorized step admits the largest prefix that fits both RPM and TPM.
5. **Simulate.** `core.sample_many(...)` is the vectorized version of the HTTP simulator's model and returns outcome, latency and actual tokens.
6. **Timing wheel.**
   - `done_tick = ⌊(now + latency) / tick⌋ + 1`.
   - `argsort` by tick, split into runs, and push each run to `wheel[tick]`.
   - In-flight cost is a few bytes per request: at 1M/s × 0.2 s, about 200k requests are in flight with no coroutine per request.
7. **Finalize.**
   - For every tick ≤ now: mark `finalized[ids] += 1` in a `uint8` bitmap, and count successes and failures.
   - Build a latency histogram with `bincount`.
   - Reconcile tokens (refund to the original bucket if it is still in the window, charge any excess to the current bucket).
8. **Report.** At the end, each worker reports its counts, the per-second completions, the histogram, the peak window usage vs. its lease, and **`exactly_once = max(finalized) ≤ 1`** plus `accounted = finalized + waiting + in_flight == generated`.

### Why the benchmark is legitimate (expect pushback here)
- A request counts only when its simulated provider call reaches a final state. Acceptance alone doesn't count, as the brief requires.
- It applies the **same** limit rules (ring, pacing, guard, reconciliation) and the **same** outcome and latency distribution as the HTTP simulator.
- **Exactly-once is verified per id**, not inferred from counters.
- Limits are raised to 1.25 × the offered load, as the brief explicitly allows, and *still enforced* per lease: `lease_respected = True` in every run.
- **Every report labels the transport "in-process".** The HTTP number (~1.1k/s per process) is published right next to it. Nothing is hidden.

### Results
- **Required:** offered 330k/s, sustained **329k/s** (lowest 1 s: 312k).
- **Stretch:** offered 1.1M/s, sustained **1.10M/s** (lowest 1 s: 1.02M).
- **Scaling (max throughput per worker count):**

| Workers | Max sustained | Efficiency |
|---|---|---|
| 1 | 1.65M/s | 100% |
| 2 | 3.30M/s | 100% |
| 4 | **4.89M/s** | 74% |
| 8 | 3.70M/s | 28% |

- **Why it drops at 8:** the machine has 8 *logical* processors, so hyper-threads share physical cores and memory bandwidth, and the OS and the parent process compete for them.
- **The 10% over-offer** makes "sustained ≥ target" a meaningful pass. Offering exactly 300k gave 299.2k sustained, which equals the offered rate but technically falls just short of 300k.

---

## 10. Scale-out plan: 10 billion requests/minute (projection)

10B/min ≈ **166.7M/s**.

1. **Partition** by model, then by request-id range within a model, into single-writer workers. Each is the benchmark worker, unchanged.
2. **Hierarchical capacity leases (Doorman).**
   - A per-model quota service leases short-TTL (~1 s) slices of each provider limit down the hierarchy: global → region → node → worker.
   - The sum of live leases never exceeds the limit.
   - Unused lease is returned at renewal.
   - The **TTL bounds limit-change propagation**: a drop is fully effective within one TTL. Grants are charged at grant time, so an outstanding lease can't overshoot past its TTL.
3. **Intake through a partitioned log** (Kafka, partitioned by model and shard). It decouples bursts from dispatch, gives replay and durability, and lets you add consumers horizontally.
4. **Provider fan-out.** No single provider account does 167M/s, so plan for many accounts or deployments, each with its own lease tree.
5. **Sizing:**
   - **Scheduling:** 166.7M / 1.22M per worker ≈ **137 workers**, about 9 nodes of 16 cores.
   - **I/O:** at an assumed ~50k req/s per core for a compiled HTTP/2 client, about **3,300 cores (~105 nodes)**.
   - **Python's per-request HTTP path** would need about 150k processes. Not viable, which is the honest conclusion.
   - So **the bottleneck at scale is network I/O, not the rate-limit logic.** The provider-facing tier should be compiled and multiplexed (Go or Rust), fed by the same lease-based ledger.
6. **Coordination traffic** is one lease renewal per worker per second, which is negligible.

---

## 11. Bugs found during testing (strong interview stories, told honestly)

| Symptom | Root cause | Fix | Lesson |
|---|---|---|---|
| Benchmark completed **0** requests (everything stuck "in flight") | `latency_ms` was `float32`. In NumPy 2, `python_float + float32_array` stays `float32`, and epoch seconds (~1.8e9) in float32 have ~128 s resolution, so the completion ticks were garbage. | Cast to `float64` before adding to the epoch time. | Watch dtype promotion with large absolute timestamps. |
| First callback attempt **4.7 s** after sealing | Sealing wrote 10k result rows with `executemany` in **autocommit** mode, so 10k fsync'd transactions blocked the event loop. | One explicit transaction, later extended to cover results + summary + outbox atomically. Now 0.2 s. | Sync I/O on an event loop; transaction boundaries also matter for crash safety. |
| Auditor crashed (`array not broadcastable`) | The simulator's audit endpoint indexed `(key, value)` tuples wrongly, sending bucket ids as counts. | `for _, v in items`. | The evidence path needs tests as much as the main path. The auditor has unit tests including an intentional violation. |
| Load generator saw "connection refused" bursts | The listen backlog overflowed during bursts of new connections. | `backlog=8192`, a steady 256-connection pool, and connect-phase-only retries. | Only retry where it's provably safe (the request never arrived). |
| Model A paced at **5,328**/min under a 5,000 limit, short-term | The pacing cap used `ceil(eff/600)`, which is 9 per bucket instead of 8.25. The window check still held the 60 s limit, but the rate was lumpy. | Exact fractional pacing: `floor((b+1)·eff/600) − floor(b·eff/600)`. | Integer rounding in rate limiters accumulates. |
| A full Scenario 1 run failed accounting (49k expired, 245 timeouts) | **The laptop suspended mid-run.** The load phase took 475 s for a 330 s schedule, so wall-clock time jumped and queued requests passed their deadlines. | `SetThreadExecutionState` keeps Windows awake during runs, and each scenario now has a "not stalled" criterion. Re-run: PASS. | Distinguish environmental failure from logic failure, and make the harness detect it. |

---

## 12. Numbers to remember

| Thing | Number |
|---|---|
| Baseline | ~600 RPM (one-at-a-time loop) |
| Effective limit with guard | 49,500 RPM = 825/s (for 50k) |
| Ring | 601 buckets × 100 ms, for requests and for tokens, per model |
| Scheduler tick | 5 ms; idle wake-up timeout 50 ms |
| Default deadline / max attempts | 60 s / 4 attempts |
| Retry backoff | 0.2 s · 2^(n−1), ±20% jitter, capped at 5 s |
| Callback backoff | 1 s · 2^(n−1), ±20%, capped at 60 s, 12 attempts, 10 s timeout |
| Provider connection pool | 2,000 concurrent; ~125 actually needed at 825/s |
| Simulated latency | 150 ms ± 50 ms, 1% tail up to +1 s |
| Scenario 1 | 98.0% utilization; auditor peak 49,539 / 50,000 (99.08%); 0 provider 429s; 329,992 requests all accounted for |
| Scenario 2 | A held 4,932/min (limit 5k), then 29,161/min after the restore; B at 100.3% |
| Scenario 3 | 282 ms ack for 10k requests; callback 503, 503, 200; body identical to the API; 10,000 unique ids |
| HTTP path ceiling | ~1.1k completed/s per control-plane process |
| Benchmark | 329k/s (required), 1.10M/s (stretch), 4.89M/s maximum with 4 workers |
| 10B/min | 166.7M/s; ~137 scheduler workers; ~3,300 I/O cores (assumed 50k/s per core) |

---

## 13. Likely questions and answers

**Design**

1. **Why not Redis for rate limiting?** A Redis round trip per decision costs about 100 µs–1 ms and adds a check-then-increment race unless you use Lua. That's why Envoy has a local limiter. Within one process the ledger is in memory and atomic by construction. Across processes and nodes I use leases, so the hot path stays local. Redis or a quota service would only hand out leases, about once per second per worker.

2. **How do you know you never exceeded the limit?** Two independent layers. The ledger enforces it at send time with a conservative 601-bucket window and a 1% guard. Separately, the provider simulator, with its own implementation, records every accepted arrival at 10 ms resolution, and the auditor recomputes every 60 s window from that alone. Peak 99.08%, zero violating windows, zero 429s.

3. **What if two requests check the ledger at the same time?** They can't. Everything runs on a single event loop thread, and `can_send` followed by `record` has no `await` between them. Across processes, each worker has its own lease, so there's no shared counter to race on.

4. **Why FIFO with head-of-line blocking? A small request could fit when a big one doesn't.** Fairness and predictability: ETAs stay monotonic, and a stream of small requests can't starve big ones. With mixed sizes, I'd move to per-size lanes or deficit round-robin. Here the binding limit is RPM, not TPM (TPM peaks at about 46%), so head-of-line blocking by tokens almost never triggers.

5. **What about priorities?** Not in scope. The slot model extends naturally: priority decides who gets the earliest slots, and on a limit drop lowest-priority requests expire first ("demand response").

6. **Why the 1% guard?** It absorbs three things: 100 ms bucket granularity, clock skew between send time and provider arrival time, and late-charged token underestimates. It costs 1% of throughput. Scenario 1 still did 98%, against a required 90%.

7. **How do you handle the estimate being wrong?** Reserve the estimate, reconcile to the actual count. Over-estimates are refunded to the original bucket while it's in the window. Under-estimates are charged to the current bucket. If estimates were wildly off, I'd keep per-model quantiles of the estimate error and size the guard from the p99 underestimate. That came up during design but is not built.

8. **What happens if the provider returns 429 anyway?** It's treated as transient: retried with backoff, and charged to the ledger, since providers count failed requests too. It shouldn't happen, because our ledger is stricter than the provider's, and it never did in any run. Detecting persistent 429s to *discover* a lower real limit (AIMD-style) was deliberately left out, because AIMD learns a limit by exceeding it, which contradicts the brief.

9. **Why reject instead of queuing everything?** An unbounded queue just converts overload into latency and memory growth until something falls over. Rejecting at admission with `Retry-After` gives back-pressure the client can act on, and keeps memory bounded at about rate × deadline.

10. **Why SQLite?** It's zero-ops, transactional, and durable enough for batches and the outbox. Per-request state stays in memory, because a write per request would cap throughput. At scale: Postgres or a log for batches, and the outbox pattern stays the same.

**Concurrency and performance**

11. **Why asyncio and not threads?** The workload is I/O-bound: thousands of concurrent waits on HTTP. Coroutines cost kilobytes, while threads cost megabytes plus context switches. A single thread also removes the need for locks on shared state. CPU-bound work, the benchmark, uses processes because of the GIL.

12. **Is the GIL a problem?** Not for the control plane, which has one thread. Its limit is single-core CPU for HTTP and JSON (~1.1k req/s). For more, run several control-plane processes, each with a capacity lease, which is the same pattern as the benchmark workers.

13. **Your benchmark doesn't do HTTP. Isn't that cheating?** The brief allows a simulated provider and raised limits, and counts a request when the simulated provider call is final, which mine does, per id, exactly once. I state the transport on every number and publish the HTTP ceiling right next to it. The benchmark proves that the scheduling logic scales to 1M/s+. The HTTP number shows where the real bottleneck is, which drives the 10B plan.

14. **Why did 8 workers get slower than 4?** 8 logical processors share fewer physical cores and memory bandwidth, and the parent process and OS compete for them. On a 16-physical-core server I'd expect near-linear scaling further out.

15. **Where's the latency at 1M/s coming from?** The simulated provider latency is about 200 ms. On top of that, each worker's 10 ms tick finalizes larger frames, so p99 grows from 335 ms to 721 ms. A smaller tick or more workers would cut it.

16. **What is coordinated omission, and why does it matter here?** A closed-loop load generator waits for responses before sending more, so when the system stalls it stops sending and the stall never shows in the stats. Mine is open-loop and measures from the intended send time, so queueing appears in p99. That's why Scenario 1's p50 is about 29 s: it is honestly queueing under 120% load.

**Correctness and failures**

17. **How do you guarantee the callback isn't sent early?** It's only enqueued by the transition that takes `remaining` to 0, in the same transaction that seals the batch. Scenario 3 checks: sealed at +16.95 s, first attempt at +17.09 s, and the last request final at or before the seal.

18. **What if the service crashes mid-batch?** Sealed batches and pending callbacks are durable and delivered after a restart. Unsealed batches are marked `interrupted`, because per-request state is in memory. The fix would be journaling admissions to a log; I cut that for scope and documented it.

19. **Can the callback be delivered twice?** Yes, it's at-least-once: for example, if the receiver times out after processing. The `Idempotency-Key` header lets the receiver deduplicate, and the mock receiver does exactly that.

20. **How do you test the limiter?** Unit tests with a fake clock:
    - A brute-force check of every 60 s window over 300 s of overload.
    - Pacing.
    - Limit drop with debt.
    - Refund to the original bucket, and dropping it after expiry.
    - Underestimate charging.
    - A zero limit.
    - Randomized token sizes.
    Plus the end-to-end auditor.

**Scale**

21. **How would you get to 10B/min?** See section 10: partition plus hierarchical leases, a log for intake, a compiled I/O tier, many provider accounts. The rule set doesn't change; only where the leases come from changes.

22. **What happens to a limit change at scale?** The quota service updates the global limit and stops renewing leases above the new share. Within one lease TTL (~1 s), every worker is on the new limit. Trailing usage counts the same way, aggregated per lease.

**Process and AI**

23. **How did you use AI?** Claude Code drove ideation, the PRD and the implementation:
    - **Ideation:** five parallel agents with different thinking frames, plus a separate fresh-context verifier.
    - **PRD:** from a brainstorm with a confirmation checkpoint.
    - **Implementation and test runs.**
    The verifier caught a real design trap: refunding tokens into the current bucket. Running everything for real surfaced the bugs in section 11. **[YOU: add what you personally decided, reviewed or changed. Only what is true.]**

24. **What would you do with more time?**
    - Move SQLite I/O off the event loop.
    - Multi-process control plane with leases.
    - Request journaling for crash-safe batches.
    - Priority lanes.
    - Separate input and output TPM.
    - HMAC-signed webhooks and a dead-letter queue with replay.
    - Run the benchmark on a 16-core Linux box.

---

## 14. Live demo runbook (short version; full narration in `LOOM_SCRIPT.md`)

**T−5 min, from `backend/`:**

```powershell
..\.venv\Scripts\python -m tools.up --dashboard --config config\demo.yaml
..\.venv\Scripts\python -m tools.loadgen --rate 1000 --duration 900 --models model-a=0.6,model-b=0.4 --tokens 1000 --token-spread 0.1 --deadline 15
```

Open http://127.0.0.1:5173 and wait about 60 s for the windows to fill.

**Demo moves, in order:**
1. **Under load.** Point at the flat blue line at about 99% under the red limit, and the gauges. Then sent/s ≈ completed/s, the rejected counter growing, and the invariant pill showing "holds".
2. **Live limit change.** On model-a, click "RPM → 5,000", then "Apply limits live". Point at the message ("N waiting expired"). A's sends drop to 0 while the window drains; model B is unaffected. Then type 30000 and apply: A recovers immediately.
3. **Batch.** Submit 2,000 with the callback URL. Point at the ack time. When the callback pill shows "delivered (3)", expand the row to show 503, 503, 200. Then open http://127.0.0.1:9100/received.
4. **Proof.** In a third terminal: `..\.venv\Scripts\python -m tools.auditor --model model-a --model model-b`. It prints the peak ratio, 0 violating windows, and exits 0.
5. **Results.** Open `docs/BENCHMARK.md`, sections 3 and 4.
6. **If asked to run something:** `..\.venv\Scripts\python -m pytest -q tests` (under 1 s), or `python -m tools.scenarios s3` (~30 s, all PASS). Stop `tools.up` first, because the scenarios use the same ports.

**If something goes wrong:**

| Problem | Fix |
|---|---|
| Dashboard shows "offline" | Restart `tools.up`. |
| Ports already in use | Close the old terminals, or end the `python`/`node` processes in Task Manager. |
| Batch is slow | Use a smaller batch, or stop the load first. |
| Questioned about a number | The raw data is in `reports/*/result.json`. Rerun the specific scenario. |

---

## 15. Honest limitations (say these before they're found)

- **Single control-plane process,** with an HTTP ceiling of about 1.1k req/s. It scales out via leases, but multi-process is not built.
- **In-memory request state.** Unsealed batches are lost on a restart and marked `interrupted`.
- **SQLite writes on the event loop thread.** Small stalls happen during large seals.
- **Throughput benchmarks are in-process,** not end-to-end HTTP, and are labelled as such everywhere.
- **Everything runs on one machine over loopback.** Real network latency is represented only by the simulated provider latency.
- **The ETA is approximate.** It affects whether a request is rejected now or expires later, never limit compliance.
- **Single RPM and TPM per model.** No separate input and output TPM, no daily limits.
- **No auth, no webhook signing, no dead-letter queue.** These are documented as production follow-ups.

---

## 16. Restructuring and rethinking the build (round-3 focus)

Expect two kinds of prompts:

- **"How would you restructure X?"** This is a design discussion. Use 16.2–16.4.
- **"Let's rebuild this part."** This may involve live coding or whiteboarding. Use 16.5.

Open every answer the same way: **name the seam, say what changes behind it, say what stays the same, then give the trade-off.**

### 16.1 How the code is split today (the seams)

| Seam (concept) | Current implementation | What already makes it swappable |
|---|---|---|
| **Capacity limiter** | `SlotLedger`: in-process ring per model | Small surface: `can_send`, `record`, `reconcile`, `set_limits`, `seconds_until_fits`. It takes an injectable `clock`, which the tests use. |
| **Scheduler / dispatcher** | `Engine._scheduler`: one coroutine per model | Talks to the limiter and the provider only through method calls |
| **Provider client** | `Engine._call_provider`: aiohttp `POST /v1/infer` | The response contract is just status code + `usage.actual_tokens` |
| **Request state store** | `Engine.requests` dict + per-model counters | The only writers are `submit`, `_send`, `_call_provider` and `_finalize` |
| **Batch store + outbox** | `BatchManager` on SQLite | Sealing is one function plus one transaction; delivery is a separate coroutine |
| **Intake** | FastAPI routes calling `engine.submit()` directly | The API layer is thin |
| **Evidence** | Simulator arrival buckets + offline auditor | Fully independent of the system under test |

**The honest weakness to admit first:** intake, scheduling, provider I/O, batch persistence and callback delivery all live in **one process on one event loop**. That makes it simple and lock-free, but it is a single failure domain with one CPU core of capacity (about 1.1k req/s over HTTP). Most restructurings below split that apart.

### 16.2 Target architecture if rebuilt for production

```
 clients ──▶ API / intake (stateless, N replicas)
                │  validate, assign id, append to log, return 202
                ▼
        durable log  (Kafka / Redis Streams / NATS JetStream), partitioned by model[:shard]
                │
                ▼
     dispatcher workers (one partition = one single writer)
        ├─ local limiter, enforcing a LEASE from ──▶ quota service (per-model ring, grants short-TTL leases,
        │                                             limits are pushed here; a hierarchy at larger scale)
        ├─ provider I/O (async; Go/Rust if > ~5k req/s per core is needed)
        └─ emits state-change events ──▶ state store (Postgres/Redis) + results (object storage for large batches)
                                              │
                                   batch aggregator (counts per batch; seals on 0)
                                              │ outbox row in the same transaction
                                              ▼
                                   callback service (N replicas, SKIP LOCKED / partitioned) ──▶ client webhooks
```

The rules don't change: the strict window, pacing, reserve-then-reconcile, rejection at the deadline, sealing once, and at-least-once callbacks. What changes is **where each rule runs and what survives a crash.**

### 16.3 Restructuring options, one by one (what, how, trade-off)

**A. Make the limiter an interface, then add a leased implementation.** This is the most likely ask.
- **What:** extract `CapacityLimiter` (`try_acquire(n, tokens) -> bucket | None`, `reconcile`, `set_limits`) with two implementations:
  - `LocalRingLimiter`: today's ledger.
  - `LeasedLimiter`: holds a slice of capacity granted by a quota service.
- **How:**
  - The quota service owns the real 601-bucket ring per model. Each grant (for example "40 requests / 40k tokens for the next 1 s") is charged into the ring **at grant time**, so outstanding grants can never exceed the limit.
  - Workers renew every ~250 ms and return unused capacity on renewal. That return is a refund into the grant's original bucket, the same rule as token reconciliation.
  - On a limit drop the service stops renewing above the new share. The change is fully effective within one TTL.
- **Trade-offs:**
  - Over-granting idle workers wastes capacity. Use demand-weighted grants based on each worker's queue depth.
  - Too short a TTL means chatty renewals; too long means slow limit changes.
  - The benchmark already proves the static-lease version (equal shares, `lease_respected=True`).
- **Why not Redis INCR per request:** a network round trip on every decision, plus a check-then-increment race unless you use Lua. Leases put the network on the renewal path (a few per second), not the request path.

**B. Split intake from dispatch with a durable log.**
- **What:** the API appends `{id, model, tokens, payload, deadline}` to a log and returns 202. Dispatcher workers consume.
- **How:**
  - Partition by model, so one model maps to one ordered partition (or `model:shard` for hot models). A partition has exactly one consumer, which preserves the single-writer, lock-free property.
  - Commit the consumer offset only after the request's final state is persisted.
- **What you gain:**
  - Crash safety: unsealed batches survive restarts, fixing the current `interrupted` limitation.
  - Burst absorption.
  - API and dispatch scale independently.
- **Trade-offs:**
  - One more component to operate.
  - Admission latency rises by a few ms.
  - Rejection at the door now needs the ETA *before* the append, so the API must read queue depth and window state from the quota service, or accept and let the worker reject. Either way it stays bounded.

**C. Move persistence off the event loop.** This is the smallest, cheapest fix, and the first thing to do.
- **What:** SQLite calls currently run synchronously on the loop. A 10k-row seal blocks it for tens of milliseconds.
- **How:**
  - Use `aiosqlite`, or `loop.run_in_executor` with a single writer thread. The existing `threading.Lock` already anticipates this.
  - For multiple replicas, move to **Postgres**: callback deliverers claim rows with `SELECT … FOR UPDATE SKIP LOCKED`.
  - Store large results as NDJSON in object storage, with a pointer in the summary.
- **Trade-off:** a little more latency per write in exchange for no loop stalls. Ordering is preserved by having a single writer per batch.

**D. Multi-process control plane on one machine.** This is the quickest way past the ~1.1k req/s HTTP ceiling.
- **Option 1:** N uvicorn workers, each holding an equal static lease. This works only if load spreads evenly, so prefer option 2.
- **Option 2 (better):** **route by model**. A thin front proxy (nginx or Envoy) routes `/v1/requests` by model to the process that owns that model's ledger. That keeps one writer per model with zero coordination; this is what the benchmark workers do.
- **Trade-off:** hot models need intra-model sharding, which brings back leases (option A).

**E. Provider I/O tier in Go or Rust.**
- **Why:** the measured bottleneck is Python HTTP and JSON, not the scheduling logic (which runs at 1.2–1.6M decisions/s per core in NumPy). At 10B/min the plan needs about 3,300 cores of I/O.
- **How:** Python keeps admission, limits, batches and the API. Dispatch workers become a Go service that consumes the log, enforces leases locally, and multiplexes HTTP/2 to the provider.
- **Trade-off:** two languages and a duplicated limiter. Mitigate with a shared spec and the same auditor running against both.

**F. Fairness and priorities.**
- **Today:** one FIFO per model. Head-of-line blocking is accepted for predictability.
- **Restructure:** per-model **lanes** (interactive, batch, retry) scheduled with **deficit round robin** or weighted fair queuing. Then a 100k-item batch can't starve single requests: give batches a configurable share, for example 30%, which they may exceed when the other lanes are idle.
- **Per-tenant quotas:** nested ledgers (tenant ring inside the model ring). Admit only if both have room.
- **Priority under limit drops:** expire the lowest priority first ("demand response").

**G. Richer limit models.**
- **Separate input and output TPM:** add a third ring. Charge output on the *estimate* or `max_tokens` and reconcile on the response, the same way as today.
- **Daily limits:** a second, coarser ring (1,440 × 1 min). Admit only if every ring has room.
- **Concurrency limits** (max in flight): a semaphore next to the rings.
- **Provider-reported headroom** (`x-ratelimit-remaining`): use it as an extra *ceiling*, never to go above the configured limit. AIMD only downward (cut on unexpected 429s, recover slowly). This differs from what was rejected earlier, which was *discovering* the limit by exceeding it.

**H. Callbacks as their own service.**
- HMAC signature with a timestamp (`webhook-signature`, Standard Webhooks).
- Per-endpoint **circuit breaker**, so one dead receiver doesn't consume attempts and worker time.
- A dead-letter state after the last attempt, plus a manual replay endpoint.
- Optional per-item streaming callbacks for very large batches.

**I. Real-time dashboard and observability.**
- Swap 1 s polling for **Server-Sent Events** or WebSocket push from the sampler.
- Export Prometheus metrics (window usage per model, queue depth, state counters, callback lag).
- Add OpenTelemetry traces across intake → dispatch → provider → callback.
- Run the auditor continuously as an alerting job ("peak ratio > 99.5%") rather than only after a run.

**J. Simulator and tests.**
- **Virtual-time mode:** the ledger already takes an injectable clock. Extend it to the engine and simulator, so a 5-minute scenario runs in seconds and deterministically in CI.
- **Chaos knobs:** the provider lowers its limit *without telling us*, latency spikes, connection resets. Test that the dispatcher degrades through retries, never by violating limits.
- **Property-based tests** (Hypothesis): random arrivals, sizes and limit changes, asserting the brute-force window check and the accounting invariant.

**K. Unify the benchmark engine and the real dispatcher.** This is the most "rethink" option.
- **Today** the HTTP path handles one request at a time, and the benchmark handles frames.
- **Restructure:** make the production dispatcher frame-based too. Each tick, admit a vectorized prefix per model (the same code as the benchmark), then hand the frame to an async I/O layer that fires the provider calls.
- **Trade-off:** one code path, so the benchmark truly measures production logic. Per-request latency gains up to one tick (5–10 ms).

### 16.4 If they ask "what would you change first?", answer in this order

1. **C**: SQLite off the loop. A small change that removes a known stall.
2. **A**: the limiter interface plus leases. It unlocks D, B and every scale story.
3. **D**: route by model to multiple processes. This gives an immediate throughput gain on one box.
4. **B**: a durable log for intake, for crash-safe batches.
5. **F**: lanes and fairness, so batches don't starve interactive traffic.
6. **E** and **K** only when measurements demand them. Don't add a second language before the Python path is saturated *after* D.

Why this order: each step is independently shippable and testable, and the existing scenario runners plus the auditor stay the regression suite throughout.

### 16.5 Parts they may ask you to rebuild live (minimal sketches)

**(1) Strict rolling-window limiter.** This is the core idea, about 30 lines.

```python
import time

class RollingLimiter:
    """Strict rolling-60s: 601 x 100 ms buckets for requests and tokens."""
    B, W = 0.1, 601

    def __init__(self, rpm, tpm, guard=0.01, clock=time.time):
        self.clock, self.req, self.tok = clock, [0] * self.W, [0] * self.W
        self.rs = self.ts = 0
        self.head = int(clock() / self.B)
        self.set_limits(rpm, tpm, guard)

    def set_limits(self, rpm, tpm, guard=0.01):
        self.rpm, self.tpm = int(rpm * (1 - guard)), int(tpm * (1 - guard))

    def _advance(self):
        b = int(self.clock() / self.B)
        while self.head < b:                       # O(1) amortised
            self.head += 1
            i = self.head % self.W
            self.rs -= self.req[i]; self.ts -= self.tok[i]
            self.req[i] = self.tok[i] = 0
        return b

    def try_acquire(self, tokens):
        b = self._advance()
        if self.rs + 1 > self.rpm or self.ts + tokens > self.tpm:
            return None
        i = b % self.W
        self.req[i] += 1; self.tok[i] += tokens; self.rs += 1; self.ts += tokens
        return b                                    # remember for reconcile

    def reconcile(self, bucket, est, actual):
        b = self._advance(); d = actual - est
        if d < 0 and bucket > b - self.W:           # refund to the ORIGINAL bucket while in window
            i = bucket % self.W; r = min(-d, self.tok[i]); self.tok[i] -= r; self.ts -= r
        elif d > 0:                                 # excess -> current bucket
            self.tok[b % self.W] += d; self.ts += d
```

Points to say while writing it:
- Why 601 buckets.
- Why the refund goes to the original bucket.
- Why there's no lock: it runs on a single event loop, or there is one per worker.
- How pacing is added: a per-bucket cap of `floor((b+1)·eff/600) − floor(b·eff/600)`.

**(2) Lease broker.** This is the multi-process version of the same rule.

```python
class QuotaService:
    def __init__(self, limiter):
        self.lim = limiter                                 # the real per-model ring lives here

    def grant(self, worker, want_req, tok_each):
        n = 0
        while n < want_req and self.lim.try_acquire(tok_each) is not None:
            n += 1                                         # charged at GRANT time
        return {"req": n, "tok_each": tok_each, "expires": time.time() + 1.0,
                "bucket": self.lim.head}                   # unused part refunded on renewal

# worker: spend the lease with a plain local counter (no network on the hot path);
# renew every ~250 ms; report unused -> service refunds into the grant's bucket if still in window.
```

Batch the grant loop in a real implementation, as one vectorized prefix admit.

**(3) Batch seal plus outbox.** This is the correctness core of the callbacks.

```python
def on_final(batch, req):
    if batch.sealed:
        return
    batch.counts[req.state] += 1
    batch.remaining -= 1
    if batch.remaining == 0:                 # exactly one transition reaches 0
        batch.sealed = True
        with db:                             # ONE transaction
            db.executemany("INSERT INTO results ...", rows)
            db.execute("UPDATE batches SET summary=?, status=? ...", (summary_json, status))
            db.execute("INSERT INTO outbox (batch_id, body, next_at, attempts) ...")
        wake_deliverer()

async def deliver(row):                      # at-least-once
    ok = await post(row.url, row.body, headers={"Idempotency-Key": row.batch_id})
    if ok:
        mark_delivered(row)
    else:
        schedule(row, delay=min(base * 2 ** row.attempts, cap) * random.uniform(0.8, 1.2))
```

**(4) Admission with a deadline (the overload policy).**

```python
wait = limiter.seconds_until_fits(1, tokens) + max(queued / rps, (queued_tokens + tokens) / tps)
if now + wait > deadline:
    return reject(retry_after=wait)          # 429; queue is bounded by construction
queue.append(req)
req.eta = now + wait                         # correctness is still enforced at send time
```

**(5) Open-loop load generator loop.**

```python
while now < end:
    due = int(rate * (now - start))
    while sent < due:                        # never waits for responses
        create_task(post(item(sent), intended_at=start + sent / rate))
        sent += 1
    await asyncio.sleep(0.002)
```

### 16.6 "Rethink" questions to be ready for

- **"Would you build it the same way again?"**
  - Keep: the ring, rejection at the deadline, the sealed statement plus outbox, and the independent auditor.
  - Change from day one: a limiter interface, persistence off the event loop, routing by model across processes, and a durable intake log, so crash-safe batches come for free.
- **"Is Python the right language?"**
  - Yes for the control plane: fast to build, and the scheduling math runs at millions of decisions per second when vectorized.
  - No for per-request provider I/O at scale. The measured ~1.1k req/s per process says so. Use a split tier, as in E.
- **"Why not just use LiteLLM / a gateway?"** Their TPM limits are best-effort: counters in Redis with a check-then-increment race, and per-worker multiplication of limits. The brief demands *no* 60 s window over the limit, which needs a strict window and an independent proof. A gateway could still sit in front for auth and routing.
- **"What breaks first at 10× load?"** In order:
  1. The single event loop's CPU (HTTP and JSON).
  2. SQLite seal stalls for very large batches.
  3. In-memory request state (bounded by eviction, but lost on a crash).
  Fixes: D → C → B.
- **"How would you make limit changes safe across many nodes?"** Push limits to the quota service only. Workers never hold more than one TTL of capacity, and the auditor checks against the max-in-window rule.

---

## 17. Personal project deep dive (frame; fill with your own project)

Use the same structure as for this project. Interviewers reward the same signals:

1. **Problem and users** (30 s). Who had the pain, and how you know.
2. **Architecture** (draw it). Components, data flow, where state lives.
3. **Two or three hard decisions.** For each: the options considered, the one you chose, why, and what it cost.
4. **Numbers.** Scale, latency, users, cost. Mark what was measured versus estimated.
5. **One hard bug or failure.** Symptom → root cause → fix → what you changed so it can't recur.
6. **What you'd restructure now.** Use the same seam → change → trade-off pattern as section 16.
7. **Your part vs. the team's or AI's.** Be specific and honest.

> **[YOU]** Fill in one line per point for your personal project before the call.
