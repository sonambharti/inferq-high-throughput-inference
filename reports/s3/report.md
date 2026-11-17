# Scenario 3: asynchronous batch completion

Generated: 2026-11-12 00:15:28  
Overall: **PASS**

## Pass criteria (measured)

| Criterion | Result | Detail |
|---|---|---|
| Initial acknowledgement within 1 second | PASS | 282 ms for 10,000 requests |
| Callback sent only after all requests reached a final state | PASS | last request final at +16.95s, sealed at +16.95s, first callback attempt at +17.09s |
| Callback delivery succeeds after the destination recovers | PASS | attempt status codes [503, 503, 200]; delivered on attempt 3 |
| Callback summary matches the batch status returned by the API | PASS | callback: status=completed_with_errors total=10000 successful=9788 failed=212; API identical=True (sha256 99c6e1a60a64...) |
| Every request id appears exactly once in the final batch result | PASS | 10,000 results, 10,000 unique request ids, all 10,000 client ids present once |

## Configuration

- 10,000 requests, alternating model-a (30k RPM) / model-b (20k RPM), 1,000 tokens
- Simulator failure injection: 5% transient (retried, up to 4 attempts), 2% permanent
- Mock receiver rejects the first 2 callback attempts (HTTP 503)
- Callback backoff: 1 s base, doubling, +/-20% jitter

## Measured results

| Metric | Value |
|---|---|
| Ack latency | 282 ms |
| Final states | {'succeeded': 9788, 'failed': 212} |
| Requests retried at least once | 528 |
| Batch status | completed_with_errors |
| Batch duration (submit to seal) | 16.692 s |
| Callback delivered | +20.47s after submit, attempt 3 |
| Request latency p50 / p95 / p99 | 6504 / 14433 / 15726 ms |

## Callback attempts

| Attempt | Time | HTTP status | Error |
|---|---|---|---|
| 1 | +17.09s | 503 |  |
| 2 | +18.31s | 503 |  |
| 3 | +20.47s | 200 |  |

## Delivered callback body

```json
{
  "batch_id": "b-69f535afc5ce",
  "counts": {
    "expired": 0,
    "failed": 212,
    "rejected": 0,
    "succeeded": 9788
  },
  "created_at": 1794422707.3736882,
  "duration_s": 16.692,
  "failed": 212,
  "results_url": "http://127.0.0.1:8000/v1/batches/b-69f535afc5ce/results",
  "sealed_at": 1794422724.0652888,
  "status": "completed_with_errors",
  "successful": 9788,
  "summary_sha256": "99c6e1a60a64dba17e0c11aa6605d88dd3d12d4bda435cce074b9d0a132e3144",
  "total": 10000
}
```

## Simulation assumptions

- Transport: **HTTP over loopback** (load generator -> control plane -> provider simulator), one machine.
- Simulated provider latency: 150 ms base, +/-50 ms uniform jitter, 1% tail up to +1 s (configurable).
- Actual tokens = estimate x U(0.8, 1.05) on success; the provider meters actual tokens.
- Latency is measured from each request's *intended* send time (open-loop), so it includes queueing.
- The dispatcher keeps a 1% guard below each limit; requests have a 60 s deadline by default.

## Environment

```
{
  "os": "Windows-11-10.0.26200-SP0",
  "python": "3.14.6",
  "cpu_count": 8,
  "machine": "AMD64",
  "processor": "Intel64 Family 6 Model 140 Stepping 1, GenuineIntel"
}
```
