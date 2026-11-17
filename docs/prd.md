---
title: High-Throughput Inference Under Provider Limits - Plan
type: feat
date: 2026-11-11
topic: high-throughput-inference
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
---

# High-Throughput Inference Under Provider Limits - Plan

## Goal Capsule

- **Objective:** Replace a ~600 RPM synchronous request loop with a Python system. It must use at least 90% of each model's provider capacity, never cross an RPM or TPM limit in any rolling 60 seconds, and complete at least 300,000 simulated requests per second. Every claim must be provable by a reviewer on their own machine.
- **Product authority:** The Consuma assignment brief (`docs/high-throughput-inference-assignment.pdf`) is the source of truth for requirements and pass criteria. This document is the source of truth for every design choice the brief leaves open.
- **Open blockers:** None.

---

## Product Contract

### Summary

We are building four components:

- **Control plane:** a Python HTTP service that accepts single and batch inference requests. It also exposes status and results, accepts limit changes while running, and delivers batch callbacks.
- **Engine:** the core that schedules every request against a per-model rolling-60-second slot ledger. That one structure acts as both the rate limiter and the overload policy.
- **Provider simulator:** used in two ways. Over HTTP, it is the realistic provider for the capacity, limit-change, and batch scenarios. Inside the engine's worker processes, it drives the 300k/s and 1M/s benchmarks.
- **Tooling:** an open-loop load generator, an independent auditor that grades each scenario PASS or FAIL from the simulator's own records, and a React dashboard for live operation and the Loom demo.

### Problem Frame

The provider grants 50,000 RPM and 100M TPM, but the current path sends one call at a time with a fixed sleep, about 600 RPM. That uses roughly 1% of paid capacity. Simply going faster is not acceptable. Limits differ per model and change while traffic is flowing. The brief requires that no observed 60-second period ever exceeds a limit. It also requires every request to be accounted for, even when demand exceeds capacity.

The submission is judged by a reviewer who will not take claims on trust. Every number must be reproducible, measurements must be kept apart from projections, and simulation assumptions must be stated. A design that only works in a narration, or whose limiter grades itself, fails that reviewer.

Three constraints shape the design:

- **Strict window.** "No 60-second period exceeds the limit" describes a sliding window. A plain token bucket with a burst allowance can violate it, so a stricter structure is needed.
- **Python at 300k/s.** A Python process handling one request object at a time tops out at tens of thousands per second. The benchmark engine must therefore process requests in vectorized frames across several processes.
- **Estimates vs actuals.** Token counts are estimates at admission and actual values at completion, so TPM accounting must reserve first and reconcile afterwards.

### Actors

- A1. **Client application.** Submits single requests or batches, polls status, and receives batch callbacks.
- A2. **Operator.** Changes model limits while the system runs and watches throughput against limits. Uses the dashboard or the API.
- A3. **Reviewer.** Clones the repository, runs the scenarios and benchmarks, and reads the generated reports and PASS/FAIL verdicts.
- A4. **Simulated provider.** Enforces its own copy of the limits, adds latency, returns success, transient failure, permanent failure, or 429, and records everything it receives.
- A5. **Callback receiver.** The client's webhook endpoint. A bundled mock version can be configured to reject the first N attempts.

### Requirements

**Request intake and status**

- R1. The system accepts a single inference request with a model, a payload, an estimated token count, an optional client request id, and an optional deadline. It returns the assigned request id and the request's initial state.
- R2. The system accepts a batch of requests with an optional `callback_url`. It returns a batch id within one second, whatever the batch size up to 100,000 requests, and before any request has been processed.
- R3. Each request is in exactly one of these states: `waiting` (admitted with a scheduled send slot), `in_flight`, `succeeded`, `failed`, `expired`, or `rejected`. `succeeded` and `failed` together form `completed`. A request is completed only when the simulated provider call reaches a final success or failure.
- R4. Clients can fetch any single request's state, result, attempt count, and timestamps by request id.
- R5. Clients can fetch any batch's status, counts, and results at any time, independently of callback delivery. Results are returned in pages.
- R6. The system continuously maintains the invariant `submitted = completed + waiting + in_flight + expired + rejected`, both per model and globally, and exposes it on a metrics endpoint.

**Limit enforcement**

- R7. Each model has its own RPM and TPM limits, loaded from a configuration file at startup.
- R8. An operator can read and change any model's limits through the API or the dashboard while the system is running, without a restart. A change to one model never affects another model's processing.
- R9. The system never sends to the provider a request that would make the number of requests or tokens sent for that model in any rolling 60 seconds exceed the limit in force.
- R10. TPM is charged from the estimated token count when a request is admitted. When the request reaches a final state, the charge is corrected to the actual token count the provider reports.
- R11. When a limit is lowered, usage already sent in the trailing 60 seconds still counts against the new limit. No new sends for that model happen until trailing usage falls below the new limit.
- R12. When a limit is raised, the extra capacity becomes usable on the next scheduling tick.

**Overload behavior (demand exceeds capacity)**

- R13. At admission, each request is either given a send slot, which is the earliest time the ledger can fit it, or rejected immediately.
- R14. A request is rejected at admission when its earliest possible slot falls after its deadline. The rejection says so and includes a retry-after hint. The default deadline is 60 seconds and is configurable per request and per model.
- R15. A waiting request expires if a limit drop pushes its slot past its deadline. The system re-projects waiting requests whenever a limit changes, so expiry happens promptly rather than at the deadline.
- R16. Transient provider failures and provider 429s are retried with backoff, up to a configurable attempt limit, and every retry is charged to the ledger. A request that runs out of attempts or deadline becomes `failed` or `expired`. Permanent failures become `failed` without retry.
- R17. The number of waiting requests per model is bounded by the deadline window. A model with a limit of 0 rejects everything, and the rejections are still fully accounted for.

**Asynchronous batches and callbacks**

- R18. A batch is sealed exactly once, at the moment its last request reaches a final state (`succeeded`, `failed`, `expired`, or `rejected`). Sealing produces an immutable summary: batch id, overall status, total, succeeded, failed, expired and rejected counts, timing, and a results URL.
- R19. The callback is sent only after sealing, and its body is the sealed summary. The batch status API returns the same sealed summary, so the two cannot disagree.
- R20. Every request id in a batch appears exactly once in the batch's final results.
- R21. Failed callback deliveries (a non-2xx response, a timeout, or a connection error) are retried with configurable exponential backoff and jitter, and every attempt carries the batch id as an idempotency key. Pending callbacks survive a service restart.
- R22. The batch status shows callback delivery state and the history of every attempt (time, response code, error).

**Provider simulator**

- R23. The simulator can configure any number of models. For each model it sets RPM and TPM limits, a latency distribution (base plus jitter, with a configurable tail), outcome probabilities (success, transient failure, permanent failure), and actual-token behavior (estimate times a configurable noise factor).
- R24. The simulator enforces its own copy of the limits over a strict rolling 60-second window and returns 429 when a request would exceed them. Simulator limits can be changed at runtime.
- R25. The simulator records arrivals and tokens per model in 100 ms buckets and serves them for auditing.
- R26. The same simulator logic runs as an HTTP service and as an in-process library. Every reported number is labelled with the transport that produced it.

**Load generator**

- R27. The load generator is open-loop: it sends on a fixed arrival schedule whether or not responses have come back. The reviewer sets the request rate, duration, model mix, single vs batch mode, batch size, approximate token size and its spread, and the deadline.
- R28. Latency is measured from each request's intended send time to its final state, so queueing delay under overload appears in the percentiles.

**Reporting and proof**

- R29. Every run produces a machine-readable result file and a Markdown report. The report covers submitted, completed, successful, failed, expired, rejected and waiting counts; completion rate; p50, p95 and p99 latency; configured and observed RPM and TPM per model over time; batch completion time; and callback timing per attempt.
- R30. An independent auditor computes, from the simulator's arrival records alone, the peak rolling-60-second requests and tokens per model, using 100 ms resolution. It checks each window against the highest limit that was in force at any point during that window. It reuses no code from the system's own limiter.
- R31. Each validation scenario runs from one command. It evaluates every pass criterion from the brief as an explicit PASS/FAIL line and exits non-zero on any failure.
- R32. Reports keep "Measured" results and "Projected" estimates in separate, clearly labelled sections, and list the simulation assumptions behind each measured number.

**Throughput benchmarks**

- R33. A benchmark command completes at least 300,000 simulated inference requests per second. "Completed" means each request id reached a final success or failure through the simulated provider, with limits raised as the brief allows. The benchmark sustains this rate for at least 30 seconds and reports the rate actually achieved.
- R34. The same benchmark attempts 1,000,000 requests per second and reports the achieved rate honestly, whether or not it reaches the target.
- R35. The benchmark measures throughput at 1, 2, 4 and 8 worker processes and reports the scaling curve.

**Dashboard (React)**

- R36. The dashboard shows each model's live observed RPM and TPM against its current limits as a time series, refreshed at least once per second.
- R37. The dashboard lets an operator change a model's RPM and TPM limits, and the change appears on the chart immediately.
- R38. The dashboard lists batches with their status, counts, and callback attempt history, and lets the user submit a demo batch.
- R39. The dashboard shows the request accounting invariant (R6) and the totals for each state.

**Documentation and deliverables**

- R40. The README explains how to install and run the service, the simulator, the load generator and the dashboard, and how to configure models and change limits. It also covers how to run each validation scenario and the benchmarks, and the key design decisions and tradeoffs.
- R41. A benchmark report (`docs/BENCHMARK.md`) records the test environment, configuration, achieved throughput, latency, failures and bottlenecks, plus the 10B-requests-per-minute scale-out plan as a labelled projection.
- R42. All scripts and configuration needed to reproduce every reported number are in the repository.

### Key Flows

- F1. **Single request**
  - **Trigger:** A client submits a request for model A.
  - **Steps:**
    1. The control plane asks model A's ledger for the earliest slot that fits one request plus the estimated tokens within both limits.
    2. If the slot is past the deadline, the request is `rejected` with retry-after. Otherwise it is `waiting` with that slot, and the response returns the slot time.
    3. At the slot time the dispatcher sends the request to the provider, and the request moves to `in_flight`.
    4. The provider answers. On success, the request becomes `succeeded` and the TPM charge is reconciled to actual tokens. On a transient failure or 429, the dispatcher books a new slot and retries. On a permanent failure, or when attempts run out, the request becomes `failed`.
  - **Outcome:** The request ends in exactly one final state, and the ledger holds its true cost.
  - **Covered by:** R1, R3, R9, R10, R13, R14, R16.

- F2. **Batch with callback**
  - **Trigger:** A client posts 10,000 requests with a `callback_url`.
  - **Steps:**
    1. The batch is persisted and its ack is returned in under one second.
    2. Requests are admitted through F1 in the background.
    3. Each final transition decrements the batch's remaining count.
    4. When the count reaches zero, the batch is sealed and an outbox entry is created.
    5. The deliverer POSTs the sealed summary and retries with backoff on failure.
    6. The client can GET the same summary at any time.
  - **Outcome:** One successful callback whose body matches the API response.
  - **Covered by:** R2, R5, R18 to R22.

- F3. **Live limit drop**
  - **Trigger:** The operator lowers model A from 30,000 to 5,000 RPM.
  - **Steps:**
    1. A new limit epoch is recorded.
    2. Model A's ledger sees that trailing-60s usage exceeds the new limit and pauses new sends until usage drains below it.
    3. Waiting requests are re-projected onto the slower schedule, and any whose new slot passes their deadline are expired.
    4. Model B's ledger is untouched.
    5. When the operator restores 30,000 RPM, model A resumes at full rate on the next tick.
  - **Outcome:** Model A stays within whichever limit is in force, and model B's throughput is unchanged.
  - **Covered by:** R8, R11, R12, R15.

### Acceptance Examples

- AE1. **Scenario 1: reach provider capacity**
  - **Covers:** R9, R10, R29, R30, R31.
  - **Given:** One model at 50,000 RPM and 100M TPM, the simulator over HTTP, and 1,000-token requests submitted at 60,000 per minute (120% of capacity) for at least 5 minutes.
  - **When:** The scenario completes.
  - **Then:** After a 30-second warm-up, completed requests per minute are at least 45,000 (90% of the allowed capacity). The auditor's peak rolling-60s value is at most 50,000 requests and at most 100M tokens. The accounting check shows every submitted id as completed, waiting, expired, or rejected, with a difference of 0.

- AE2. **Scenario 2: different models and a changing limit**
  - **Covers:** R8, R11, R12, R15, R29, R30.
  - **Given:**
    - Model A at 30,000 RPM / 60M TPM and model B at 20,000 RPM / 40M TPM, both overdriven.
    - Model A is lowered to 5,000 RPM at minute 2 and restored at minute 4, in a 6-minute run.
  - **When:** The scenario completes.
  - **Then:**
    - Each model's peak rolling-60s usage stays within the highest limit in force during that window.
    - During its constrained period, after the window drains, model A's throughput settles at about 5,000 RPM, and it returns to about 30,000 RPM after the restore.
    - Model B's per-minute throughput stays within 5% of its pre-change level throughout.
    - The report shows configured limits and observed throughput as time series for both models.

- AE3. **Scenario 3: asynchronous batch completion**
  - **Covers:** R2, R18 to R22.
  - **Given:**
    - 10,000 requests across models A and B with a `callback_url` pointing at the mock receiver, which rejects its first two attempts.
    - The simulator is configured with 5% transient and 2% permanent failures.
  - **When:** The batch is submitted and processing completes.
  - **Then:**
    - The ack arrived in under 1 second.
    - The callback is first attempted only after the batch is sealed.
    - Attempts 1 and 2 are recorded as rejected, and attempt 3 succeeds.
    - The delivered body equals the GET batch response field for field.
    - The results contain 10,000 unique request ids, with no missing ids and no duplicates.

- AE4. **Throughput benchmark**
  - **Covers:** R33, R34, R35.
  - **Given:** In-process simulator, raised limits, and 8 worker processes on the reviewer's machine.
  - **When:** `bench --target 300000 --duration 30` runs.
  - **Then:** The report shows at least 300,000 completed requests per second, sustained, with a completion count per final state and the transport label "in-process". The 1M attempt and the 1/2/4/8-process scaling curve appear alongside it.

### Success Criteria

- All three validation scenarios print PASS on every criterion from a clean clone on a laptop-class machine (8 cores), each with one command.
- The 300k/s benchmark passes on the same machine. The 1M/s result is reported as measured, whatever it is.
- A reviewer can follow the README from clone to the first scenario PASS in under 10 minutes, not counting the scenarios' own run time.
- An implementer can build every component from this document without inventing request states, overload behavior, callback semantics, or pass criteria.

### Key Decisions

- **Python backend and React dashboard.** (session-settled: user-directed: chosen over Go, which was recommended for raw throughput: the user wants Python for the backend and React.js for the frontend, built end-to-end from scratch.) Consequence: the 300k/s and 1M/s benchmarks rely on vectorized frame processing across multiple processes, not one Python object per request.
- **Strict slot ledger instead of a token bucket.** Each model has two rings of 100 ms buckets, one for requests and one for tokens, covering 60 seconds plus a guard bucket. A request is admitted only when both rings stay within limit for every 60-second window containing its slot. Reason: a token bucket with burst can break a strict rolling window, and fixed windows allow up to twice the limit across a boundary.
- **Booking a slot is the overload policy.** Admission returns a slot time or an immediate rejection. Reason: this maps directly onto the brief's four accounting states, keeps queues bounded by construction, and gives clients an honest ETA.
- **Reconciliation direction.** If actual tokens are below the estimate, the difference is refunded to the request's original bucket if that bucket is still inside the window, and dropped otherwise. If actual tokens are above the estimate, the excess is charged to the current bucket. Reason: refunding into the current bucket would understate trailing usage and allow overshoot.
- **Limit-drop semantics.** A lowered limit applies to sends after the change, and trailing usage still counts against it. The auditor checks each window against the highest limit in force during that window. Reason: under a literal reading, any drop below current trailing usage is impossible to satisfy instantly. The README states this rule.
- **Two transports, one simulator.** HTTP is used for the capacity, limit-change, and batch scenarios and the demo. In-process frames are used for the throughput benchmarks. Every number is labelled with its transport. Reason: per-request HTTP in Python cannot approach 300k/s. Labelling keeps the benchmark honest rather than suspect.
- **Independent auditor.** Compliance is graded from the provider simulator's own arrival records, not from the limiter's counters. Reason: a limiter that grades itself is not evidence.
- **Persistence scope.** Batches, sealed summaries, results, and the callback outbox are stored in SQLite. Hot-path request state is held in memory. Reason: callbacks must survive a restart (R21), and a database write per request would cap throughput.
- **Shared-nothing per-model scheduling.** Each model's ledger and dispatch loop are independent. Reason: this isolates model B from changes to model A (R8). For multi-process scale it extends to short-lived capacity leases (see the scale-out plan).

### Scope Boundaries

- Real paid LLM calls are out. Only the simulator is called.
- Discovering limits automatically from provider 429s or rate-limit headers is out. Limits come from configuration and the operator, and the simulator's 429s are handled as retryable failures.
- Webhook signing (HMAC), dead-letter queues, and manual callback replay are out. They are mentioned only as production follow-ups in the README.
- Authentication, multi-tenancy, and per-tenant quotas are out.
- Multi-node deployment is out of the build. It appears only as the projected 10B-requests-per-minute plan.
- Separate input and output TPM limits, and daily limits, are out. Each model has one RPM and one TPM limit.
- Durable request-level state across restarts is out. Only batch state and the callback outbox are durable.

### Dependencies / Assumptions

- Python 3.11 or newer and Node 18 or newer are available. The reference machine is Windows 11 with 8 logical cores, Python 3.14 and Node 24.
- Simulated provider latency defaults to 150 ms base, 50 ms jitter and a 1% tail of up to 1 s. This is an assumption, not a measurement of any real provider, and it is configurable.
- The simulator's actual tokens equal the estimate times a uniform factor in [0.8, 1.05] by default, so reconciliation mostly refunds.
- The provider capacity scenarios run over loopback HTTP on one machine. Network latency to a real provider is represented only by the simulator's configured latency.
- The benchmark's in-process simulator applies the same outcome and latency model in virtual time per frame. It does not sleep per request.

### Outstanding Questions

#### Deferred to Planning

- [Affects R33][Technical] The frame size and the number of worker processes that maximise throughput on the reference machine. Determined by measurement.
- [Affects R36][Technical] Push (server-sent events) vs polling for dashboard updates. Either meets the 1-second refresh requirement.

---

## Design Notes (for the implementer)

These notes record design choices the brainstorm settled at a level below the Product Contract, so the implementation does not have to rediscover them.

### Components

| Component | Role | Transport |
|---|---|---|
| Control plane (FastAPI) | Intake, status, limits API, metrics, batch store, callback deliverer | HTTP :8000 |
| Engine (in control plane) | Per-model slot ledger and dispatch loop, retries, state table | in-process |
| Provider simulator (FastAPI) | Enforces limits, simulates latency and outcomes, records arrivals | HTTP :9000 |
| Auditor | Reads simulator arrival buckets and limit history, computes peak rolling-60s, produces PASS/FAIL | CLI |
| Load generator | Open-loop scheduler for single or batch traffic | CLI → HTTP |
| Mock callback receiver | Rejects the first N attempts, then records deliveries | HTTP :9100 |
| Benchmark engine | Multi-process vectorized frames: ledger admit → simulate → finalize | in-process |
| Dashboard (React + Vite) | Live charts, limit editor, batches view, accounting | HTTP :5173 |

### Slot ledger (per model)

- Time is divided into 100 ms buckets. Each model holds two rings of 601 buckets: request counts and token counts.
- For a single request the dispatch loop admits greedily: send while both trailing 600-bucket sums plus the request's cost stay within limit. Otherwise wait until enough capacity ages out.
- Slot projection for the ETA and deadline check is computed from the current queue depth and the current rate: `eta = now + queued_ahead / (limit_per_bucket)`. The ETA is advisory. Correctness comes from the send-time check.
- The ledger records sends at the moment the dispatcher hands the request to the provider. A guard margin, 1% of the limit by default and configurable, absorbs any skew between the dispatcher's clock and the simulator's arrival timestamps.
- A limit change replaces the ledger's limits atomically and appends an epoch `(timestamp, rpm, tpm)` to the limit history. The simulator receives the same change so its own enforcement and the auditor stay aligned.

### Vectorized benchmark (300k/s to 1M/s)

- Each worker process owns a slice of the request-id space and holds a capacity lease for each model, a share of the raised limit valid for one tick.
- Per tick, which defaults to 10 ms:
  1. Generate a frame of N requests as NumPy arrays: ids, model, estimated tokens, and arrival times.
  2. Admit the largest prefix that fits the lease, using a cumulative sum against the remaining request and token budgets. The rest wait for the next tick, or are rejected past their deadline.
  3. Apply the simulator's outcome and latency model to the admitted arrays.
  4. Place requests in a timing wheel keyed on completion tick.
  5. Finalize every request whose completion tick has arrived. Final counts per state and latency histograms are accumulated as arrays.
- The parent process aggregates per-worker counters every second and computes completed per second. The run fails its own conservation check if any id is missing or finalized twice. This is verified through per-worker id-range counts.

### 10B requests/minute plan (projected, not measured)

- 10B/min is about 167M completions/s. The plan scales the measured per-process rate from R35 out horizontally:
  - **Shard by model and by request-id range.** Within one model, capacity is split into short-TTL leases granted by a per-model ledger service (the Doorman pattern), so each node enforces locally and the global sum never exceeds the limit.
  - **Hierarchy.** A global quota per provider account is leased to regions, regions lease to nodes, and nodes lease to worker processes. The lease TTL bounds how quickly a limit drop propagates.
  - **Provider fan-out.** No single provider account offers 167M/s, so the plan assumes many accounts or deployments, each with its own ledger.
  - **Intake.** Requests are partitioned through a log (for example, Kafka with partitions per model and shard), so intake is decoupled from dispatch.
- The node count is computed as `167M / (measured per-process rate × processes per node × scaling efficiency)`. This figure appears in `docs/BENCHMARK.md` under "Projected".

### Sources

- `docs/high-throughput-inference-assignment.pdf`: assignment brief and pass criteria.
- OpenAI rate-limit and Batch API docs: reserve-at-admission token accounting and batch status model.
- Anthropic rate-limit and Message Batches docs: continuous token buckets and per-item batch states.
- YouTube Doorman design: leased, locally-enforced distributed quota.
- Cloudflare, "Counting things, a lot of different things": the approximation error of sliding-window counters.
- Svix retry schedule: webhook retry conventions.
- Gil Tene, coordinated omission (wrk2, HdrHistogram): why the load generator is open-loop.
