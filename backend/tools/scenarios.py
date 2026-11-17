"""One-command validation scenarios with machine-checked PASS/FAIL criteria.

  python -m tools.scenarios s1            # provider capacity, 50k RPM / 100M TPM, >= 5 min
  python -m tools.scenarios s2            # two models, Model A limit drop + restore
  python -m tools.scenarios s3            # 10k async batch, failures, callback rejected twice
  python -m tools.scenarios all
  add --quick for a shortened dev run (NOT a valid submission run: durations < 5 min)

Each scenario starts its own provider simulator, control plane (and mock callback
receiver), drives load, audits the simulator's evidence independently, prints a
PASS/FAIL line per criterion, and writes reports/<scenario>/{report.md,result.json,*.svg}.
Exit code is non-zero if any criterion fails.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

from tools import auditor
from tools.charts import line_chart
from tools.loadgen import parse_mix, run_load

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT.parent / "reports"
PY = sys.executable
SIM_URL, API_URL, RCV_URL = "http://127.0.0.1:9000", "http://127.0.0.1:8000", "http://127.0.0.1:9100"


# --------------------------------------------------------------------- helpers
def keep_awake() -> None:
    """Ask Windows not to sleep while this process runs (a suspended machine invalidates a
    timed run: wall-clock jumps expire queued requests).  No-op elsewhere; ends with the process."""
    if os.name == "nt":
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            pass


def stall_check(checks: "Checks", st, duration: float) -> None:
    wall = st.ended_at - st.started_at
    checks.add("Run was not stalled by the environment (sleep / suspend)",
               wall <= duration + 30,
               f"load phase took {wall:.0f}s for a {duration:.0f}s schedule"
               + ("" if wall <= duration + 30 else " - machine was suspended or starved; re-run"))


def http(method: str, url: str, body: dict | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_health(url: str, timeout: float = 30) -> None:
    t = time.time()
    while time.time() - t < timeout:
        try:
            http("GET", f"{url}/health", timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"service at {url} did not become healthy")


class Stack:
    """Starts simulator + control plane (+ mock receiver) as subprocesses."""

    def __init__(self, config: dict, reject_first: int | None = None, seed: int = 7):
        self.tmp = tempfile.mkdtemp(prefix="inferq-")
        self.cfg_path = os.path.join(self.tmp, "models.yaml")
        with open(self.cfg_path, "w") as f:
            yaml.safe_dump(config, f)
        self.reject_first = reject_first
        self.seed = seed
        self.procs: list[subprocess.Popen] = []

    def __enter__(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        logs = open(os.path.join(self.tmp, "services.log"), "w")
        self.procs.append(subprocess.Popen([PY, "-m", "simulator.server", "--port", "9000", "--config",
                                            self.cfg_path, "--seed", str(self.seed)], cwd=ROOT, env=env,
                                           stdout=logs, stderr=logs))
        self.procs.append(subprocess.Popen([PY, "-m", "inferq.api", "--port", "8000", "--config", self.cfg_path,
                                            "--db", os.path.join(self.tmp, "inferq.db"),
                                            "--callback-backoff", "1.0"],
                                           cwd=ROOT, env=env, stdout=logs, stderr=logs))
        if self.reject_first is not None:
            self.procs.append(subprocess.Popen([PY, "-m", "tools.mock_receiver", "--port", "9100",
                                                "--reject-first", str(self.reject_first)], cwd=ROOT, env=env,
                                               stdout=logs, stderr=logs))
            wait_health(RCV_URL)
        wait_health(SIM_URL)
        wait_health(API_URL)
        return self

    def __exit__(self, *exc):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


class Checks:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.items.append({"criterion": name, "pass": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    @property
    def ok(self) -> bool:
        return all(i["pass"] for i in self.items)


def environment() -> dict:
    return {"os": platform.platform(), "python": platform.python_version(), "cpu_count": os.cpu_count(),
            "machine": platform.machine(), "processor": platform.processor()}


def series_from_timeseries(points: list[dict], model: str, key: str, t0: float):
    pts = [p for p in points if p["model"] == model]
    return [p["t"] - t0 for p in pts], [p[key] for p in pts]


def rolling_minute(xs: list[float], ys: list[float]) -> tuple[list, list]:
    """Per-second counts -> trailing-60s sums (requests per minute)."""
    out_x, out_y, acc = [], [], []
    for x, y in zip(xs, ys):
        acc.append(y)
        if len(acc) > 60:
            acc.pop(0)
        out_x.append(x)
        out_y.append(sum(acc))
    return out_x, out_y


def write_report(name: str, title: str, checks: Checks, sections: list[tuple[str, str]], result: dict) -> Path:
    d = REPORTS / name
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", "", f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}  ",
             f"Overall: **{'PASS' if checks.ok else 'FAIL'}**", "", "## Pass criteria (measured)", "",
             "| Criterion | Result | Detail |", "|---|---|---|"]
    for c in checks.items:
        lines.append(f"| {c['criterion']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail']} |")
    for h, body in sections:
        lines += ["", f"## {h}", "", body]
    (d / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result["checks"] = checks.items
    result["pass"] = checks.ok
    (d / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"  report: {d / 'report.md'}")
    return d


def fmt_lat(lat: dict) -> str:
    return " / ".join(f"{lat.get(k)}" for k in ("p50_ms", "p95_ms", "p99_ms")) + " ms"


ASSUMPTIONS = (
    "- Transport: **HTTP over loopback** (load generator -> control plane -> provider simulator), one machine.\n"
    "- Simulated provider latency: 150 ms base, +/-50 ms uniform jitter, 1% tail up to +1 s (configurable).\n"
    "- Actual tokens = estimate x U(0.8, 1.05) on success; the provider meters actual tokens.\n"
    "- Latency is measured from each request's *intended* send time (open-loop), so it includes queueing.\n"
    "- The dispatcher keeps a 1% guard below each limit; requests have a 60 s deadline by default."
)


# ------------------------------------------------------------------ scenario 1
def scenario1(quick: bool) -> bool:
    print("\n=== Scenario 1: reach the available provider capacity (50k RPM / 100M TPM) ===")
    duration = 120 if quick else 330
    warmup = 30
    rate = 1000.0  # 60k/min = 120% of the 50k RPM limit
    cfg = {"defaults": {"deadline_s": 60, "max_attempts": 4, "guard_fraction": 0.01},
           "models": {"model-a": {"rpm": 50000, "tpm": 100_000_000}},
           "simulator": {"default": {"latency_ms": 150, "jitter_ms": 50, "tail_prob": 0.01, "tail_ms": 1000,
                                     "p_transient": 0.0, "p_permanent": 0.0, "token_noise": [0.8, 1.05]}}}
    checks = Checks()
    with Stack(cfg):
        print(f"  driving {rate:.0f} req/s (120% of limit) of 1000-token requests for {duration}s ...")
        t0 = time.time()
        st = asyncio.run(run_load(API_URL, rate, duration, parse_mix("model-a=1"), 1000, 0.0))
        time.sleep(2)
        metrics = http("GET", f"{API_URL}/v1/metrics")
        ts = http("GET", f"{API_URL}/v1/metrics/timeseries")["points"]
        audit = auditor.audit(auditor.fetch(SIM_URL, "model-a"))

    tot = metrics["models"]["model-a"]
    xs, comp = series_from_timeseries(ts, "model-a", "completed", t0)
    post = [y for x, y in zip(xs, comp) if warmup <= x <= duration]
    avg_per_min = (sum(post) / max(len(post), 1)) * 60
    rx, ry = rolling_minute(xs, comp)
    full_windows = [y for x, y in zip(rx, ry) if x >= warmup + 60 and x <= duration]
    min_window = min(full_windows) if full_windows else None

    checks.add("Completion >= 90% of allowed capacity after warm-up",
               avg_per_min >= 0.9 * 50000,
               f"{avg_per_min:,.0f} completed/min after {warmup}s warm-up = {avg_per_min / 50000:.1%} of 50,000 "
               f"(lowest full 60 s window: {min_window if min_window is None else f'{min_window:,}'})")
    checks.add("No observed 60 s window exceeds the RPM limit (auditor)",
               audit["violating_windows_rpm"] == 0,
               f"peak {audit['peak_window_requests']:,} req / 60 s vs limit {audit['limit_rpm_at_peak']:,.0f} "
               f"({audit['peak_rpm_ratio']:.2%}); {audit['windows_checked']:,} windows at 10 ms resolution")
    checks.add("No observed 60 s window exceeds the TPM limit (auditor)",
               audit["violating_windows_tpm"] == 0,
               f"peak {audit['peak_window_tokens']:,} tokens / 60 s vs limit {audit['limit_tpm_at_peak']:,.0f} "
               f"({audit['peak_tpm_ratio']:.2%})")
    states = {k: tot[k] for k in ("succeeded", "failed", "waiting", "in_flight", "expired", "rejected")}
    acct = tot["submitted"] == sum(states.values()) and st.submitted == tot["submitted"] and st.http_errors == 0
    checks.add("Every submitted request is accounted for",
               acct,
               f"load generator acked {st.submitted:,} (transport errors {st.http_errors}, safe connect retries "
               f"{st.connect_retries}); server submitted "
               f"{tot['submitted']:,} = completed {tot['completed']:,} + waiting {tot['waiting'] + tot['in_flight']:,} "
               f"+ expired {tot['expired']:,} + rejected {tot['rejected']:,}")
    stall_check(checks, st, duration)
    if quick:
        checks.add("Duration >= 5 minutes", False, "--quick run: re-run without --quick for a valid result")

    d = REPORTS / "s1"
    d.mkdir(parents=True, exist_ok=True)
    ax, ay = rolling_minute(list(range(len(audit["series"]["per_second_requests"]))),
                            audit["series"]["per_second_requests"])
    (d / "rpm.svg").write_text(line_chart([
        {"name": "observed req / trailing 60 s (provider)", "x": ax, "y": ay},
        {"name": "RPM limit", "x": [0, ax[-1] if ax else 1], "y": [50000, 50000], "dashed": True, "color": "#dc2626"},
        {"name": "completed / trailing 60 s", "x": rx, "y": ry, "color": "#16a34a"},
    ], "Scenario 1: requests per rolling minute vs limit", "requests / 60 s"), encoding="utf-8")
    lat = metrics["latency"]["model-a"]
    sections = [
        ("Configuration", f"- Model `model-a`: 50,000 RPM, 100,000,000 TPM\n- Offered load: {rate:.0f} req/s "
                          f"(= {rate * 60:,.0f}/min, 120% of capacity), 1,000 estimated tokens each\n"
                          f"- Duration: {duration} s, warm-up excluded: {warmup} s"),
        ("Measured results", f"| Metric | Value |\n|---|---|\n| Submitted (acked) | {st.submitted:,} |\n"
                             f"| Completed | {tot['completed']:,} |\n| Succeeded | {tot['succeeded']:,} |\n"
                             f"| Failed | {tot['failed']:,} |\n| Waiting at end (incl. in flight) | "
                             f"{tot['waiting'] + tot['in_flight']:,} |\n| Expired | {tot['expired']:,} |\n"
                             f"| Rejected at admission | {tot['rejected']:,} |\n| Completion rate after warm-up | "
                             f"{avg_per_min:,.0f}/min |\n| Latency p50 / p95 / p99 (from intended send) | "
                             f"{fmt_lat(lat)} |\n| Provider 429s | "
                             f"{audit['provider_counters'].get('rate_limited')} |\n"
                             f"| Peak rolling-60 s requests (provider) | {audit['peak_window_requests']:,} |\n"
                             f"| Peak rolling-60 s tokens (provider) | {audit['peak_window_tokens']:,} |"),
        ("Observed throughput vs limit", "![rpm](rpm.svg)"),
        ("Why latency is high", "The offered load is 120% of capacity, so the excess queues until the 60 s "
                                "deadline and is then rejected at admission. p50/p95 therefore reflect queueing "
                                "by design; provider latency itself is ~150 ms."),
        ("Simulation assumptions", ASSUMPTIONS),
        ("Environment", "```\n" + json.dumps(environment(), indent=2) + "\n```"),
    ]
    write_report("s1", "Scenario 1: reach the available provider capacity", checks, sections,
                 {"loadgen": {k: v for k, v in st.__dict__.items() if k not in ("per_second", "batches")},
                  "metrics": metrics, "audit": {k: v for k, v in audit.items() if k != "series"}})
    return checks.ok


# ------------------------------------------------------------------ scenario 2
def scenario2(quick: bool) -> bool:
    print("\n=== Scenario 2: different models and changing limits ===")
    if quick:
        duration, t_drop, t_restore = 220, 70, 150
    else:
        duration, t_drop, t_restore = 360, 120, 240
    cfg = {"defaults": {"deadline_s": 60, "max_attempts": 4, "guard_fraction": 0.01},
           "models": {"model-a": {"rpm": 30000, "tpm": 60_000_000}, "model-b": {"rpm": 20000, "tpm": 40_000_000}},
           "simulator": {"default": {"latency_ms": 150, "jitter_ms": 50, "tail_prob": 0.01, "tail_ms": 1000,
                                     "p_transient": 0.0, "p_permanent": 0.0, "token_noise": [0.8, 1.05]}}}
    checks = Checks()
    events = []
    with Stack(cfg):
        rate = 1000.0  # 600/s model-a (120% of 30k RPM) + 400/s model-b (120% of 20k RPM)
        print(f"  driving {rate:.0f} req/s (A 60% / B 40%, each 120% of its limit) for {duration}s; "
              f"A -> 5,000 RPM at t={t_drop}s, restored at t={t_restore}s")
        t0 = time.time()

        async def drive():
            load = asyncio.create_task(run_load(API_URL, rate, duration, parse_mix("model-a=0.6,model-b=0.4"),
                                                1000, 0.0))
            await asyncio.sleep(t_drop - (time.time() - t0))
            r = await asyncio.to_thread(http, "PUT", f"{API_URL}/v1/models/model-a/limits",
                                        {"rpm": 5000, "tpm": 60_000_000})
            events.append({"t": time.time() - t0, "action": "model-a -> 5,000 RPM", "response": r})
            print(f"  t={time.time() - t0:.0f}s limit drop applied: {r}")
            await asyncio.sleep(t_restore - (time.time() - t0))
            r = await asyncio.to_thread(http, "PUT", f"{API_URL}/v1/models/model-a/limits",
                                        {"rpm": 30000, "tpm": 60_000_000})
            events.append({"t": time.time() - t0, "action": "model-a -> 30,000 RPM", "response": r})
            print(f"  t={time.time() - t0:.0f}s limit restored: {r}")
            return await load

        st = asyncio.run(drive())
        time.sleep(2)
        metrics = http("GET", f"{API_URL}/v1/metrics")
        ts = http("GET", f"{API_URL}/v1/metrics/timeseries")["points"]
        audits = {m: auditor.audit(auditor.fetch(SIM_URL, m)) for m in ("model-a", "model-b")}

    for m, a in audits.items():
        checks.add(f"{m} stays within its current limits (auditor)",
                   a["pass"], f"peak {a['peak_window_requests']:,} req/60 s vs max limit in window "
                              f"{a['limit_rpm_at_peak']:,.0f} ({a['peak_rpm_ratio']:.2%}); TPM peak "
                              f"{a['peak_tpm_ratio']:.2%}; violating windows {a['violating_windows_rpm']}"
                              f"/{a['violating_windows_tpm']}")

    def sent_rate(model, a, b):
        xs, ys = series_from_timeseries(ts, model, "sent", t0)
        v = [y for x, y in zip(xs, ys) if a <= x < b]
        return sum(v) / max(len(v), 1) * 60

    a_constrained = sent_rate("model-a", t_drop + 65, t_restore) if t_restore - t_drop > 70 else None
    xa, wa = series_from_timeseries(ts, "model-a", "window_requests", t0)
    win_before_restore = max([w for x, w in zip(xa, wa) if t_restore - 3 <= x < t_restore] or [0])
    if a_constrained is None:
        a_constrained = win_before_restore
    a_after = sent_rate("model-a", t_restore + 10, duration)
    a_before = sent_rate("model-a", 30, t_drop)
    checks.add("Model A adapts to the reduction without restart",
               0.8 * 5000 <= a_constrained <= 5000,
               f"A send rate while constrained (after window drained): {a_constrained:,.0f}/min (limit 5,000); "
               f"A trailing-60 s window just before restore: {win_before_restore:,}; "
               f"before: {a_before:,.0f}/min")
    checks.add("Model A adapts to the restore without restart",
               a_after >= 0.85 * 30000,
               f"A send rate after restore: {a_after:,.0f}/min (limit 30,000)")
    b_before = sent_rate("model-b", 30, t_drop)
    b_during = sent_rate("model-b", t_drop, t_restore)
    checks.add("Model B continues processing while A is constrained",
               b_during >= 0.95 * b_before and b_during > 0,
               f"B send rate before: {b_before:,.0f}/min, during A's constraint: {b_during:,.0f}/min "
               f"({b_during / max(b_before, 1):.1%})")
    checks.add("Report shows configured limits and observed throughput over time", True,
               "see rpm.svg and the timeline table in reports/s2/report.md")
    stall_check(checks, st, duration)
    if quick:
        checks.add("Full-length run", False, "--quick run: re-run without --quick for a valid result")

    d = REPORTS / "s2"
    d.mkdir(parents=True, exist_ok=True)
    series = []
    for m, color in (("model-a", "#2563eb"), ("model-b", "#16a34a")):
        a = audits[m]
        ax, ay = rolling_minute(list(range(len(a["series"]["per_second_requests"]))),
                                a["series"]["per_second_requests"])
        off = a["series"]["t0"] - t0
        series.append({"name": f"{m} observed (rolling 60 s)", "x": [x + off for x in ax], "y": ay, "color": color})
        lx, ly = series_from_timeseries(ts, m, "rpm_limit", t0)
        series.append({"name": f"{m} RPM limit", "x": lx, "y": ly, "dashed": True, "color": color})
    (d / "rpm.svg").write_text(line_chart(series, "Scenario 2: rolling-minute requests vs configured limits",
                                          "requests / 60 s"), encoding="utf-8")
    inst = []
    for m, color in (("model-a", "#2563eb"), ("model-b", "#16a34a")):
        xs, ys = series_from_timeseries(ts, m, "sent", t0)
        inst.append({"name": f"{m} sends / s", "x": xs, "y": ys, "color": color})
    (d / "rate.svg").write_text(line_chart(inst, "Scenario 2: sends per second", "requests / s"), encoding="utf-8")

    rows = ["| t (s) | A limit | A sent/s | A window (60 s) | B limit | B sent/s | B window (60 s) |",
            "|---|---|---|---|---|---|---|"]
    by_t: dict[int, dict] = {}
    for p in ts:
        by_t.setdefault(int(p["t"] - t0), {})[p["model"]] = p
    for t in sorted(by_t):
        if t % 15 == 0 and "model-a" in by_t[t] and "model-b" in by_t[t]:
            a, b = by_t[t]["model-a"], by_t[t]["model-b"]
            rows.append(f"| {t} | {a['rpm_limit']:,} | {a['sent']} | {a['window_requests']:,} | {b['rpm_limit']:,} | "
                        f"{b['sent']} | {b['window_requests']:,} |")
    lat = metrics["latency"]
    sections = [
        ("Configuration", f"- model-a: 30,000 RPM / 60M TPM; model-b: 20,000 RPM / 40M TPM\n"
                          f"- Offered: 600 req/s to A and 400 req/s to B (each 120% of its limit), 1,000 tokens\n"
                          f"- Limit events: " + "; ".join(f"t={e['t']:.0f}s {e['action']}" for e in events)),
        ("Measured results", "| Model | Submitted | Completed | Waiting | Expired | Rejected | p50 / p95 / p99 |\n"
                             "|---|---|---|---|---|---|---|\n" + "\n".join(
            f"| {m} | {c['submitted']:,} | {c['completed']:,} | {c['waiting'] + c['in_flight']:,} | {c['expired']:,} "
            f"| {c['rejected']:,} | {fmt_lat(lat[m])} |" for m, c in metrics["models"].items())),
        ("Limits and observed throughput over time", "![rpm](rpm.svg)\n\n![rate](rate.svg)\n\n" + "\n".join(rows)),
        ("How the drop is handled", "Sends already made in the trailing 60 s still count against the new 5,000 RPM "
                                    "limit, so model A sends nothing until its window drains below 5,000, then "
                                    "paces at ~83/s. Waiting requests are re-projected on the change and those "
                                    "whose new slot falls past their deadline are expired immediately. The "
                                    "auditor checks each window against the highest limit in force during it."),
        ("Simulation assumptions", ASSUMPTIONS),
        ("Environment", "```\n" + json.dumps(environment(), indent=2) + "\n```"),
    ]
    write_report("s2", "Scenario 2: different models and changing limits", checks, sections,
                 {"events": events, "metrics": metrics,
                  "audit": {m: {k: v for k, v in a.items() if k != "series"} for m, a in audits.items()}})
    return checks.ok


# ------------------------------------------------------------------ scenario 3
def scenario3(quick: bool) -> bool:
    print("\n=== Scenario 3: asynchronous batch completion ===")
    cfg = {"defaults": {"deadline_s": 120, "max_attempts": 4, "guard_fraction": 0.01},
           "models": {"model-a": {"rpm": 30000, "tpm": 60_000_000}, "model-b": {"rpm": 20000, "tpm": 40_000_000}},
           "simulator": {"default": {"latency_ms": 150, "jitter_ms": 50, "tail_prob": 0.01, "tail_ms": 1000,
                                     "p_transient": 0.05, "p_permanent": 0.02, "token_noise": [0.8, 1.05]}}}
    checks = Checks()
    n = 10_000
    with Stack(cfg, reject_first=2):
        items = [{"model": "model-a" if i % 2 == 0 else "model-b", "estimated_tokens": 1000,
                  "payload": {"prompt": f"batch item {i}"}, "request_id": f"client-{i}"} for i in range(n)]
        t_submit = time.time()
        ack = http("POST", f"{API_URL}/v1/batches", {"requests": items, "callback_url": f"{RCV_URL}/callback"})
        ack_s = time.time() - t_submit
        bid = ack["batch_id"]
        print(f"  batch {bid} acknowledged in {ack_s * 1000:.0f} ms; waiting for completion + callback ...")
        status = None
        deadline = time.time() + 600
        while time.time() < deadline:
            status = http("GET", f"{API_URL}/v1/batches/{bid}")
            cb = status.get("callback") or {}
            if status.get("sealed_at") and cb.get("status") in ("delivered", "failed"):
                break
            time.sleep(1)
        received = http("GET", f"{RCV_URL}/received")
        results, offset = [], 0
        while offset is not None:
            page = http("GET", f"{API_URL}/v1/batches/{bid}/results?offset={offset}&limit=5000")
            results += page["results"]
            offset = page.get("next_offset")

    cb = status["callback"]
    checks.add("Initial acknowledgement within 1 second", ack_s < 1.0, f"{ack_s * 1000:.0f} ms for {n:,} requests")
    final_states = {"succeeded", "failed", "expired", "rejected"}
    all_final = all(r["state"] in final_states for r in results)
    last_final = max((r["finished_at"] or 0) for r in results) if results else 0
    first_attempt = cb["history"][0]["at"] if cb["history"] else None
    checks.add("Callback sent only after all requests reached a final state",
               all_final and first_attempt is not None and first_attempt >= status["sealed_at"] >= last_final,
               f"last request final at +{last_final - t_submit:.2f}s, sealed at +{status['sealed_at'] - t_submit:.2f}s, "
               f"first callback attempt at +{(first_attempt or 0) - t_submit:.2f}s")
    codes = [h["status_code"] for h in cb["history"]]
    checks.add("Callback delivery succeeds after the destination recovers",
               cb["status"] == "delivered" and codes[:2] == [503, 503] and bid in received["delivered"],
               f"attempt status codes {codes}; delivered on attempt {cb['attempts']}")
    delivered = received["delivered"].get(bid, {}).get("body", {})
    api_summary = {k: v for k, v in status.items() if k != "callback"}
    checks.add("Callback summary matches the batch status returned by the API",
               delivered == api_summary,
               f"callback: status={delivered.get('status')} total={delivered.get('total')} "
               f"successful={delivered.get('successful')} failed={delivered.get('failed')}; "
               f"API identical={delivered == api_summary} (sha256 {str(api_summary.get('summary_sha256'))[:12]}...)")
    ids = [r["request_id"] for r in results]
    client_ids = sorted(r["client_request_id"] for r in results)
    checks.add("Every request id appears exactly once in the final batch result",
               len(ids) == n and len(set(ids)) == n and client_ids == sorted(f"client-{i}" for i in range(n)),
               f"{len(ids):,} results, {len(set(ids)):,} unique request ids, all {n:,} client ids present once")

    by_state: dict[str, int] = {}
    for r in results:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    retried = sum(1 for r in results if r["attempts"] > 1)
    lat = sorted(r["latency_ms"] for r in results if r["latency_ms"] is not None)
    pct = (lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None)
    rows = "\n".join(f"| {h['attempt']} | +{h['at'] - t_submit:.2f}s | {h['status_code']} | {h['error'] or ''} |"
                     for h in cb["history"])
    sections = [
        ("Configuration", "- 10,000 requests, alternating model-a (30k RPM) / model-b (20k RPM), 1,000 tokens\n"
                          "- Simulator failure injection: 5% transient (retried, up to 4 attempts), 2% permanent\n"
                          "- Mock receiver rejects the first 2 callback attempts (HTTP 503)\n"
                          "- Callback backoff: 1 s base, doubling, +/-20% jitter"),
        ("Measured results", f"| Metric | Value |\n|---|---|\n| Ack latency | {ack_s * 1000:.0f} ms |\n"
                             f"| Final states | {by_state} |\n| Requests retried at least once | {retried:,} |\n"
                             f"| Batch status | {status['status']} |\n| Batch duration (submit to seal) | "
                             f"{status['duration_s']} s |\n| Callback delivered | +{(cb['delivered_at'] or 0) - t_submit:.2f}s "
                             f"after submit, attempt {cb['attempts']} |\n| Request latency p50 / p95 / p99 | "
                             f"{pct(.5):.0f} / {pct(.95):.0f} / {pct(.99):.0f} ms |"),
        ("Callback attempts", "| Attempt | Time | HTTP status | Error |\n|---|---|---|---|\n" + rows),
        ("Delivered callback body", "```json\n" + json.dumps(delivered, indent=2) + "\n```"),
        ("Simulation assumptions", ASSUMPTIONS),
        ("Environment", "```\n" + json.dumps(environment(), indent=2) + "\n```"),
    ]
    write_report("s3", "Scenario 3: asynchronous batch completion", checks, sections,
                 {"batch_id": bid, "ack_ms": ack_s * 1000, "status": status, "received": received,
                  "states": by_state})
    return checks.ok


def main() -> None:
    ap = argparse.ArgumentParser(description="Run validation scenarios")
    ap.add_argument("scenario", choices=["s1", "s2", "s3", "all"])
    ap.add_argument("--quick", action="store_true", help="shortened dev run (not valid for submission)")
    a = ap.parse_args()
    keep_awake()
    fns = {"s1": scenario1, "s2": scenario2, "s3": scenario3}
    todo = ["s1", "s2", "s3"] if a.scenario == "all" else [a.scenario]
    results = {s: fns[s](a.quick) for s in todo}
    print("\nSummary: " + ", ".join(f"{s}={'PASS' if ok else 'FAIL'}" for s, ok in results.items()))
    raise SystemExit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
