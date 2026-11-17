"""Open-loop load generator.

Sends on a fixed arrival schedule regardless of how fast responses come back
(no coordinated omission).  Every request carries `intended_at`, its scheduled
send time; the control plane measures latency from that instant to the final
state, so queueing under overload shows up in p95/p99.

Examples:
  python -m tools.loadgen --rate 1000 --duration 330 --models model-a=1 --tokens 1000
  python -m tools.loadgen --rate 1000 --duration 60 --models model-a=0.6,model-b=0.4 \
         --mode batch --batch-size 100 --callback-url http://127.0.0.1:9100/callback
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field

import aiohttp


@dataclass
class LoadStats:
    submitted: int = 0          # requests the control plane acknowledged (any state)
    accepted: int = 0           # acknowledged as waiting
    rejected: int = 0           # acknowledged as rejected (429 / no capacity before deadline)
    http_errors: int = 0        # transport errors / unexpected status (NOT accounted by the server)
    batches: list = field(default_factory=list)
    ack_ms: list = field(default_factory=list)
    connect_retries: int = 0
    error_kinds: dict = field(default_factory=dict)
    lag_ms_max: float = 0.0     # worst gap between intended and actual send
    per_second: dict = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = 0.0


def parse_mix(s: str) -> list[tuple[str, float]]:
    out = []
    for part in s.split(","):
        name, _, w = part.partition("=")
        out.append((name.strip(), float(w or 1)))
    tot = sum(w for _, w in out)
    return [(n, w / tot) for n, w in out]


def pick(mix, r: float) -> str:
    acc = 0.0
    for name, w in mix:
        acc += w
        if r <= acc:
            return name
    return mix[-1][0]


async def run_load(url: str, rate: float, duration: float, mix, tokens: int, spread: float,
                   mode: str = "single", batch_size: int = 100, callback_url: str | None = None,
                   deadline_s: float | None = None, max_conns: int = 256, seed: int = 1,
                   progress: bool = True, rate_schedule=None) -> LoadStats:
    """rate_schedule: optional list of (start_second, rate) overriding `rate` from that second."""
    rnd = random.Random(seed)
    st = LoadStats()
    conn = aiohttp.TCPConnector(limit=max_conns, limit_per_host=max_conns)
    session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=60))
    tasks: set[asyncio.Task] = set()
    url = url.rstrip("/")

    def make_item(i: int, intended: float) -> dict:
        tok = tokens if spread <= 0 else max(1, int(rnd.gauss(tokens, tokens * spread)))
        item = {"model": pick(mix, rnd.random()), "estimated_tokens": tok,
                "payload": {"prompt": f"load request {i}"}, "request_id": f"lg-{seed}-{i}",
                "intended_at": intended}
        if deadline_s:
            item["deadline_s"] = deadline_s
        return item

    async def send_single(item: dict):
        # A connect-phase failure means the request never reached the server, so it is
        # safe to retry (no double submission).  Anything later is counted as an error.
        for attempt in range(5):
            try:
                return await _send_single(item)
            except aiohttp.ClientConnectorError:
                st.connect_retries += 1
                await asyncio.sleep(0.05 * (attempt + 1))
        st.http_errors += 1
        st.error_kinds["connect_failed_after_retries"] = st.error_kinds.get("connect_failed_after_retries", 0) + 1

    async def _send_single(item: dict):
        try:
            async with session.post(f"{url}/v1/requests", json=item) as r:
                await r.read()
                if r.status in (200, 202):
                    st.submitted += 1
                    st.accepted += 1
                elif r.status == 429:
                    st.submitted += 1
                    st.rejected += 1
                else:
                    st.http_errors += 1
                    st.error_kinds[str(r.status)] = st.error_kinds.get(str(r.status), 0) + 1
        except aiohttp.ClientConnectorError:
            raise
        except Exception as e:
            st.http_errors += 1
            k = f"{e.__class__.__name__}: {str(e)[:80]}"
            st.error_kinds[k] = st.error_kinds.get(k, 0) + 1

    async def send_batch(items: list[dict]):
        t = time.time()
        body = {"requests": items}
        if callback_url:
            body["callback_url"] = callback_url
        if deadline_s:
            body["deadline_s"] = deadline_s
        try:
            async with session.post(f"{url}/v1/batches", json=body) as r:
                data = await r.json(content_type=None)
                if r.status in (200, 202):
                    st.submitted += len(items)
                    st.accepted += len(items)
                    st.ack_ms.append((time.time() - t) * 1000)
                    st.batches.append({"batch_id": data["batch_id"], "size": len(items), "ack_ms": st.ack_ms[-1],
                                       "submitted_at": t})
                else:
                    st.http_errors += len(items)
        except Exception:
            st.http_errors += len(items)

    st.started_at = start = time.time()
    sent = 0
    sched = sorted(rate_schedule or [(0, rate)])
    end = start + duration
    last_print = start
    unit = batch_size if mode == "batch" else 1

    def target_count(now: float) -> float:
        # integral of the piecewise-constant rate schedule from start to now
        el = now - start
        total = 0.0
        for k, (s0, r0) in enumerate(sched):
            s1 = sched[k + 1][0] if k + 1 < len(sched) else float("inf")
            if el <= s0:
                break
            total += (min(el, s1) - s0) * r0
        return total

    def intended_time(i: int) -> float:
        # invert the schedule: time at which request i was due
        acc = 0.0
        for k, (s0, r0) in enumerate(sched):
            s1 = sched[k + 1][0] if k + 1 < len(sched) else float("inf")
            seg = (s1 - s0) * r0
            if acc + seg > i or s1 == float("inf"):
                return start + s0 + (i - acc) / max(r0, 1e-9)
            acc += seg
        return start

    while True:
        now = time.time()
        if now >= end:
            break
        due = int(target_count(now))
        while sent + unit <= due or (sent < due and mode == "single"):
            if mode == "single":
                it = make_item(sent, intended_time(sent))
                st.lag_ms_max = max(st.lag_ms_max, (now - it["intended_at"]) * 1000)
                t = asyncio.create_task(send_single(it))
                sent += 1
            else:
                items = [make_item(sent + j, intended_time(sent + j)) for j in range(unit)]
                t = asyncio.create_task(send_batch(items))
                sent += unit
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        sec = int(now - start)
        st.per_second[sec] = sent
        if progress and now - last_print >= 5:
            last_print = now
            print(f"  [loadgen] t={now - start:5.0f}s sent={sent} acked={st.submitted} "
                  f"rejected={st.rejected} errors={st.http_errors} in-flight={len(tasks)}", file=sys.stderr)
        await asyncio.sleep(0.002)
    if tasks:
        await asyncio.wait(tasks, timeout=120)
    st.ended_at = time.time()
    await session.close()
    return st


def main() -> None:
    ap = argparse.ArgumentParser(description="Open-loop load generator")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--rate", type=float, default=1000, help="requests per second (total)")
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--models", default="model-a=1", help="model mix, e.g. model-a=0.6,model-b=0.4")
    ap.add_argument("--tokens", type=int, default=1000, help="approximate tokens per request")
    ap.add_argument("--token-spread", type=float, default=0.0, help="relative std-dev of token size")
    ap.add_argument("--mode", choices=["single", "batch"], default="single")
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--callback-url", default=None)
    ap.add_argument("--deadline", type=float, default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=None, help="write JSON stats here")
    a = ap.parse_args()
    st = asyncio.run(run_load(a.url, a.rate, a.duration, parse_mix(a.models), a.tokens, a.token_spread,
                              a.mode, a.batch_size, a.callback_url, a.deadline, seed=a.seed))
    res = {k: v for k, v in st.__dict__.items() if k != "per_second"}
    print(json.dumps({k: v for k, v in res.items() if k not in ("batches", "ack_ms")}, indent=2))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)


if __name__ == "__main__":
    main()
