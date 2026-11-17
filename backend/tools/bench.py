"""High-throughput simulation benchmark (in-process transport).

Why in-process: per-request HTTP + JSON in Python tops out at a few thousand
requests/s per process, far below 300k/s.  The benchmark therefore runs the
*same* scheduling rules (strict rolling-60s ledger with pacing, admission with
deadline, reserve-then-reconcile tokens) and the *same* provider outcome model
(simulator.core.sample_many) inside worker processes, on columnar frames of
requests (NumPy), with simulated provider latency held in a timing wheel.

Each worker owns a capacity LEASE: an equal share of every model limit.  The
sum of leases never exceeds the configured limit, so the global limit holds
without any cross-process coordination (the same idea scales to nodes/regions
in the 10B/min plan).

A request counts as completed only when its simulated provider call reaches a
final success/failure.  Every worker verifies that each generated request id was
finalised exactly once (or is still accounted as waiting / in flight).

Examples:
  python -m tools.bench --target 300000 --duration 30 --workers 8
  python -m tools.bench --target 1000000 --duration 30 --workers 8
  python -m tools.bench --scaling --duration 10          # max throughput at 1/2/4/8 workers
  python -m tools.bench --all                            # everything above + reports/bench/report.md
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import platform
import time
from pathlib import Path

import numpy as np

REPORTS = Path(__file__).resolve().parents[2] / "reports"
RING = 601
WIN = 600
MAX_LAT_MS = 120_000


# ============================================================== worker process
def worker(wid: int, start_at: float, duration: float, rate: float, lease_rpm: int, lease_tpm: int,
           sim: dict, tokens: int, deadline_s: float, tick_s: float, max_frame: int, out_q) -> None:
    from inferq.config import SimModelConfig
    from simulator.core import SUCCESS, sample_many

    cfg = SimModelConfig.from_dict(sim)
    rng = np.random.default_rng(1000 + wid)
    eff_rpm, eff_tpm = int(lease_rpm * 0.99), int(lease_tpm * 0.99)
    req_ring = np.zeros(RING, np.int64)
    tok_ring = np.zeros(RING, np.int64)
    req_sum = tok_sum = 0
    peak_window_req = peak_window_tok = 0

    # id space for the exactly-once bitmap (1 byte per request id)
    per_s = rate if math.isfinite(rate) else 3_000_000
    cap_ids = min(int(per_s * (duration + 5)) + max_frame * 4, 200_000_000)
    finalized = np.zeros(cap_ids, np.uint8)
    hist = np.zeros(MAX_LAT_MS + 1, np.int64)

    pend_ids = np.empty(0, np.int64)
    pend_est = np.empty(0, np.int64)
    pend_arr = np.empty(0, np.float64)
    wheel: dict[int, list] = {}
    in_wheel = 0
    generated = succeeded = failed = expired = rejected = 0
    sec_completed: dict[int, int] = {}

    while time.time() < start_at:
        time.sleep(0.001)
    t_start = time.time()
    head = int(t_start * 10)
    last_tick = int(t_start / tick_s)
    end = t_start + duration
    gen_stopped = False

    while True:
        now = time.time()
        if now >= end:
            gen_stopped = True
        # ----------------------------------------------- generate (open loop)
        if not gen_stopped:
            due = int(rate * (now - t_start)) if math.isfinite(rate) else generated + max_frame
            n_new = min(max(0, due - generated), max_frame, cap_ids - generated)
            if n_new:
                ids = np.arange(generated, generated + n_new, dtype=np.int64)
                arr = t_start + ids / rate if math.isfinite(rate) else np.full(n_new, now)
                generated += n_new
                if pend_ids.size:
                    pend_ids = np.concatenate([pend_ids, ids])
                    pend_est = np.concatenate([pend_est, np.full(n_new, tokens, np.int64)])
                    pend_arr = np.concatenate([pend_arr, arr])
                else:
                    pend_ids, pend_est, pend_arr = ids, np.full(n_new, tokens, np.int64), arr
        # ------------------------------------------------- advance the ledger
        b = int(now * 10)
        if b - head >= RING:
            req_ring[:] = 0
            tok_ring[:] = 0
            req_sum = tok_sum = 0
            head = b
        while head < b:
            head += 1
            i = head % RING
            req_sum -= int(req_ring[i])
            tok_sum -= int(tok_ring[i])
            req_ring[i] = 0
            tok_ring[i] = 0
        cur = b % RING
        # ------------------------------------------------------- expire stale
        if pend_ids.size and pend_arr[0] < now - deadline_s:
            k = int(np.searchsorted(pend_arr, now - deadline_s))
            finalized[pend_ids[:k]] += 1
            expired += k
            pend_ids, pend_est, pend_arr = pend_ids[k:], pend_est[k:], pend_arr[k:]
        # ----------------------------------------------------------- admit
        if pend_ids.size:
            # exact fractional pacing, identical to inferq.ledger.SlotLedger._bucket_cap
            cap_req_b = (b + 1) * eff_rpm // WIN - b * eff_rpm // WIN
            cap_tok_b = (b + 1) * eff_tpm // WIN - b * eff_tpm // WIN
            room_req = min(eff_rpm - req_sum, cap_req_b - int(req_ring[cur]))
            room_tok = min(eff_tpm - tok_sum, cap_tok_b - int(tok_ring[cur]))
            k = min(pend_ids.size, max(0, room_req))
            if k > 0:
                cs = np.cumsum(pend_est[:k])
                k = int(np.searchsorted(cs, room_tok, side="right"))
            if k > 0:
                ids, est, arr = pend_ids[:k], pend_est[:k], pend_arr[:k]
                pend_ids, pend_est, pend_arr = pend_ids[k:], pend_est[k:], pend_arr[k:]
                tok_k = int(cs[k - 1])
                req_ring[cur] += k
                tok_ring[cur] += tok_k
                req_sum += k
                tok_sum += tok_k
                peak_window_req = max(peak_window_req, req_sum)
                peak_window_tok = max(peak_window_tok, tok_sum)
                outcome, lat_ms, actual = sample_many(cfg, est, rng)
                # float64: epoch seconds do not fit float32 precision (NumPy 2 keeps float32 otherwise)
                done_tick = ((now + lat_ms.astype(np.float64) / 1000.0) / tick_s).astype(np.int64) + 1
                order = np.argsort(done_tick, kind="stable")
                dt_sorted = done_tick[order]
                cuts = np.flatnonzero(np.diff(dt_sorted)) + 1
                starts = np.concatenate([[0], cuts])
                stops = np.concatenate([cuts, [k]])
                for s0, s1 in zip(starts, stops):
                    sl = order[s0:s1]
                    wheel.setdefault(int(dt_sorted[s0]), []).append(
                        (ids[sl], outcome[sl], arr[sl], est[sl], actual[sl], b))
                in_wheel += k
        # ------------------------------------- finalise completed provider calls
        tick_now = int(now / tick_s)
        for t in range(last_tick + 1, tick_now + 1):
            chunks = wheel.pop(t, None)
            if not chunks:
                continue
            for ids, outc, arr, est, actual, bkt in chunks:
                finalized[ids] += 1
                ok = int((outc == SUCCESS).sum())
                succeeded += ok
                failed += ids.size - ok
                in_wheel -= ids.size
                lat = np.clip(((now - arr) * 1000).astype(np.int64), 0, MAX_LAT_MS)
                hist += np.bincount(lat, minlength=MAX_LAT_MS + 1)
                # reconcile reserved estimate -> metered actual tokens
                delta = actual - est
                refund = int(-delta[delta < 0].sum())
                extra = int(delta[delta > 0].sum())
                if refund and bkt > head - RING:
                    j = bkt % RING
                    refund = min(refund, int(tok_ring[j]))
                    tok_ring[j] -= refund
                    tok_sum -= refund
                if extra:
                    tok_ring[cur] += extra
                    tok_sum += extra
            sec = int(now - t_start)
            sec_completed[sec] = sec_completed.get(sec, 0) + sum(c[0].size for c in chunks)
        last_tick = tick_now
        if gen_stopped and in_wheel == 0:
            break
        if gen_stopped and now > end + 5:   # drain cap
            break
        nxt = (tick_now + 1) * tick_s
        slack = nxt - time.time()
        if slack > 0:
            time.sleep(slack)

    waiting = int(pend_ids.size)
    fin = finalized[:generated]
    exactly_once = bool(fin.max(initial=0) <= 1)
    final_count = int(fin.sum())
    out_q.put({
        "wid": wid, "generated": generated, "succeeded": succeeded, "failed": failed, "expired": expired,
        "rejected": rejected, "waiting": waiting, "in_flight": in_wheel,
        "exactly_once": exactly_once,
        "accounted": final_count + waiting + in_wheel == generated,
        "per_second": sec_completed, "hist": hist.tolist() if hist.any() else [],
        "peak_window_req": peak_window_req, "eff_rpm": eff_rpm,
        "peak_window_tok": peak_window_tok, "eff_tpm": eff_tpm,
        "t_start": t_start,
    })


# ============================================================== orchestration
def run(target: float, duration: float, workers: int, tokens: int = 1000, max_frame: int = 50_000,
        tick_s: float = 0.01, sim: dict | None = None, label: str = "") -> dict:
    sim = sim or {"latency_ms": 150, "jitter_ms": 50, "tail_prob": 0.01, "tail_ms": 1000,
                  "p_transient": 0.0, "p_permanent": 0.01, "token_noise": [0.8, 1.05]}
    per_worker = target / workers if math.isfinite(target) else math.inf
    # raised limits (allowed by the brief): 1.25x the offered load, split into equal leases
    total_rpm = int((target if math.isfinite(target) else 5_000_000 * workers) * 60 * 1.25)
    total_tpm = total_rpm * tokens * 2
    lease_rpm, lease_tpm = total_rpm // workers, total_tpm // workers
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    start_at = time.time() + 2.0 + 0.25 * workers
    procs = [ctx.Process(target=worker, args=(w, start_at, duration, per_worker, lease_rpm, lease_tpm, sim,
                                              tokens, 60.0, tick_s, max_frame, q)) for w in range(workers)]
    for p in procs:
        p.start()
    res = [q.get() for _ in procs]
    for p in procs:
        p.join()

    per_sec: dict[int, int] = {}
    for r in res:
        off = int(round(r["t_start"] - start_at))
        for s, c in r["per_second"].items():
            per_sec[int(s) + off] = per_sec.get(int(s) + off, 0) + c
    secs = sorted(s for s in per_sec if s < int(duration))
    steady = [per_sec[s] for s in secs if s >= 2]          # skip the first 2 s ramp
    hist = np.zeros(MAX_LAT_MS + 1, np.int64)
    for r in res:
        if r["hist"]:
            hist += np.asarray(r["hist"], np.int64)

    def pct(p):
        if hist.sum() == 0:
            return None
        return int(np.searchsorted(np.cumsum(hist), p * hist.sum()))

    tot = {k: sum(r[k] for r in res) for k in ("generated", "succeeded", "failed", "expired", "rejected",
                                                "waiting", "in_flight")}
    completed = tot["succeeded"] + tot["failed"]
    out = {
        "label": label, "transport": "in-process (vectorised frames, simulated provider in worker processes)",
        "target_rps": target if math.isfinite(target) else "max", "duration_s": duration, "workers": workers,
        "tokens_per_request": tokens, "configured_rpm": total_rpm, "configured_tpm": total_tpm,
        "lease_rpm_per_worker": lease_rpm, **tot, "completed": completed,
        "sustained_rps_median": int(np.median(steady)) if steady else 0,
        "sustained_rps_min": int(min(steady)) if steady else 0,
        "mean_rps_over_run": int(sum(per_sec[s] for s in secs) / max(len(secs), 1)),
        "per_second": [per_sec[s] for s in secs],
        "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "p99": pct(0.99)},
        "exactly_once": all(r["exactly_once"] for r in res),
        "all_accounted": all(r["accounted"] for r in res),
        "lease_respected": all(r["peak_window_req"] <= r["eff_rpm"] and r["peak_window_tok"] <= r["eff_tpm"]
                               for r in res),
        "simulator": sim,
    }
    print(f"  [{label or 'run'}] workers={workers} target={out['target_rps']} -> sustained median "
          f"{out['sustained_rps_median']:,}/s (min {out['sustained_rps_min']:,}/s), completed {completed:,}, "
          f"exactly-once={out['exactly_once']} accounted={out['all_accounted']} p50/p95/p99="
          f"{out['latency_ms']['p50']}/{out['latency_ms']['p95']}/{out['latency_ms']['p99']} ms")
    return out


def environment() -> dict:
    return {"os": platform.platform(), "python": platform.python_version(), "numpy": np.__version__,
            "logical_cpus": os.cpu_count(), "machine": platform.machine(), "processor": platform.processor()}


def write_report(results: dict) -> Path:
    from tools.charts import line_chart

    d = REPORTS / "bench"
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    lines = ["# Throughput benchmark (in-process transport)", "",
             f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
             "## Measured", "", "| Run | Workers | Target req/s | Sustained median req/s | Min 1 s req/s | "
             "Completed | p50 / p95 / p99 ms | Exactly-once | Accounted | Lease respected |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    runs = [r for k, r in results.items() if k.startswith("run_")] + results.get("scaling", [])
    for r in runs:
        lat = r["latency_ms"]
        lines.append(f"| {r['label']} | {r['workers']} | {r['target_rps']:,} | **{r['sustained_rps_median']:,}** | "
                     f"{r['sustained_rps_min']:,} | {r['completed']:,} | {lat['p50']} / {lat['p95']} / {lat['p99']} | "
                     f"{r['exactly_once']} | {r['all_accounted']} | {r['lease_respected']} |"
                     if isinstance(r['target_rps'], (int, float)) else
                     f"| {r['label']} | {r['workers']} | max | **{r['sustained_rps_median']:,}** | "
                     f"{r['sustained_rps_min']:,} | {r['completed']:,} | {lat['p50']} / {lat['p95']} / {lat['p99']} | "
                     f"{r['exactly_once']} | {r['all_accounted']} | {r['lease_respected']} |")
    for key, fname in (("run_300k", "rate_300k.svg"), ("run_1m", "rate_1m.svg")):
        r = results.get(key)
        if r:
            xs = list(range(len(r["per_second"])))
            (d / fname).write_text(line_chart(
                [{"name": "completed / s", "x": xs, "y": r["per_second"]},
                 {"name": "goal", "x": [0, xs[-1] if xs else 1], "y": [300_000 if key == "run_300k" else 1_000_000] * 2, "dashed": True,
                  "color": "#dc2626"}],
                f"{r['label']}: completed requests per second", "completed / s"), encoding="utf-8")
            lines += ["", f"![{key}]({fname})"]
    sc = results.get("scaling")
    if sc:
        (d / "scaling.svg").write_text(line_chart(
            [{"name": "max sustained completed / s", "x": [r["workers"] for r in sc],
              "y": [r["sustained_rps_median"] for r in sc]}],
            "Scaling curve: max throughput vs worker processes", "completed / s", x_label="worker processes"),
            encoding="utf-8")
        base = sc[0]["sustained_rps_median"]
        lines += ["", "### Scaling curve", "", "![scaling](scaling.svg)", "",
                  "| Workers | Max sustained req/s | Per worker | Efficiency vs 1 worker |", "|---|---|---|---|"]
        for r in sc:
            per = r["sustained_rps_median"] / r["workers"]
            lines.append(f"| {r['workers']} | {r['sustained_rps_median']:,} | {per:,.0f} | {per / max(base, 1):.0%} |")
    lines += ["", "## Simulation assumptions", "",
              "- Transport is **in-process**: the provider simulator's outcome/latency model runs inside each "
              "worker on NumPy frames. No HTTP/JSON per request; these numbers are NOT end-to-end HTTP numbers "
              "(see Scenario 1 for the HTTP path).",
              "- Limits raised as permitted by the brief: RPM = 1.25 x offered load, split into equal per-worker "
              "leases; ledger rules (601 x 100 ms rolling window, pacing, 1% guard, reconcile) are enforced "
              "per lease.",
              "- Simulated latency 150 ms +/- 50 ms, 1% tail up to +1 s, 1% permanent failures; completion is "
              "timed by a 10 ms timing wheel, so latency includes up to one tick of scheduling delay.",
              "- Every request id is checked to be finalised exactly once (or still accounted as waiting/in flight).",
              "", "## Environment", "", "```", json.dumps(environment(), indent=2), "```"]
    (d / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  report: {d / 'report.md'}")
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="In-process throughput benchmark")
    ap.add_argument("--target", type=float, default=300_000, help="offered requests per second (total)")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--tokens", type=int, default=1000)
    ap.add_argument("--scaling", action="store_true", help="max throughput at 1/2/4/8 workers")
    ap.add_argument("--all", action="store_true", help="300k + 1M + scaling, and write reports/bench")
    a = ap.parse_args()
    from tools.scenarios import keep_awake
    keep_awake()
    results: dict = {"environment": environment()}
    if a.all:
        results["run_300k"] = run(330_000, a.duration, a.workers, a.tokens, label="300k/s required (offered 330k/s)")
        results["run_1m"] = run(1_100_000, a.duration, a.workers, a.tokens, label="1M/s stretch (offered 1.1M/s)")
        results["scaling"] = [run(math.inf, 10, w, a.tokens, label=f"max@{w}") for w in (1, 2, 4, 8)
                              if w <= (os.cpu_count() or 1)]
        write_report(results)
        ok = (results["run_300k"]["sustained_rps_median"] >= 300_000 and results["run_300k"]["exactly_once"]
              and results["run_300k"]["all_accounted"])
        print(f"\n300k/s requirement: {'PASS' if ok else 'FAIL'}; 1M/s stretch: "
              f"{results['run_1m']['sustained_rps_median']:,}/s sustained")
        raise SystemExit(0 if ok else 1)
    if a.scaling:
        results["scaling"] = [run(math.inf, a.duration, w, a.tokens, label=f"max@{w}") for w in (1, 2, 4, 8)]
        write_report(results)
        return
    r = run(a.target, a.duration, a.workers, a.tokens, label=f"{int(a.target):,}/s")
    print(json.dumps({k: v for k, v in r.items() if k != "per_second"}, indent=2))


if __name__ == "__main__":
    main()
