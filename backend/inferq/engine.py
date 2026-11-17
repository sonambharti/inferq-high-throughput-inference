"""Dispatch engine: request state table, per-model schedulers, retries, accounting.

States:  waiting -> in_flight -> succeeded | failed      (completed = succeeded + failed)
         waiting -> expired        (deadline passed before a send slot was available)
         rejected                  (refused at admission: no slot before the deadline)

Invariant (checked continuously, exposed on /v1/metrics):
    submitted == waiting + in_flight + succeeded + failed + expired + rejected
"""
from __future__ import annotations

import asyncio
import itertools
import math
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from inferq.config import AppConfig
from inferq.ledger import SlotLedger

STATES = ("waiting", "in_flight", "succeeded", "failed", "expired", "rejected")
FINAL_STATES = {"succeeded", "failed", "expired", "rejected"}
TICK_S = 0.005


class Request:
    __slots__ = (
        "id", "client_id", "model", "est_tokens", "payload", "batch_id", "deadline", "intended_at",
        "created_at", "state", "attempts", "eta", "not_before", "sent_at", "bucket", "finished_at",
        "output", "error", "actual_tokens", "reason",
    )

    def __init__(self, rid, client_id, model, est_tokens, payload, batch_id, deadline, intended_at, now):
        self.id = rid
        self.client_id = client_id
        self.model = model
        self.est_tokens = est_tokens
        self.payload = payload
        self.batch_id = batch_id
        self.deadline = deadline
        self.intended_at = intended_at or now
        self.created_at = now
        self.state = "waiting"
        self.attempts = 0
        self.eta = None
        self.not_before = 0.0
        self.sent_at = None
        self.bucket = None
        self.finished_at = None
        self.output = None
        self.error = None
        self.actual_tokens = None
        self.reason = None

    def to_dict(self) -> dict[str, Any]:
        lat = None
        if self.finished_at is not None and self.state in ("succeeded", "failed"):
            lat = round((self.finished_at - self.intended_at) * 1000, 2)
        return {
            "request_id": self.id,
            "client_request_id": self.client_id,
            "model": self.model,
            "batch_id": self.batch_id,
            "state": self.state,
            "attempts": self.attempts,
            "estimated_tokens": self.est_tokens,
            "actual_tokens": self.actual_tokens,
            "output": self.output,
            "error": self.error,
            "reason": self.reason,
            "created_at": self.created_at,
            "eta": self.eta,
            "finished_at": self.finished_at,
            "latency_ms": lat,
        }


@dataclass
class ModelRuntime:
    name: str
    ledger: SlotLedger
    deadline_s: float
    queue: deque = field(default_factory=deque)       # FIFO of waiting Requests
    retry: deque = field(default_factory=deque)       # retries (served first once not_before passes)
    queued_tokens: int = 0
    counts: dict = field(default_factory=lambda: {s: 0 for s in ("submitted",) + STATES})
    # per-second activity counters (reset by the sampler every second)
    sec: dict = field(default_factory=lambda: {"sent": 0, "sent_tokens": 0, "completed": 0,
                                               "succeeded": 0, "failed": 0, "expired": 0,
                                               "rejected": 0, "retries": 0, "rate_limited": 0})
    latencies_ms: list = field(default_factory=list)
    wake: asyncio.Event | None = None


class Engine:
    def __init__(self, cfg: AppConfig, provider_url: str, max_concurrency: int = 2000,
                 on_final: Callable[[Request], None] | None = None):
        self.cfg = cfg
        self.provider_url = provider_url.rstrip("/")
        self.max_concurrency = max_concurrency
        self.on_final = on_final
        self.models: dict[str, ModelRuntime] = {}
        self.requests: dict[str, Request] = {}
        self.timeseries: deque = deque(maxlen=1800)
        self.limit_events: list[dict] = []
        self._session: aiohttp.ClientSession | None = None
        self._tasks: list[asyncio.Task] = []
        self._inflight = 0
        self._seq = itertools.count(1)
        self._rand = random.Random()
        self.started_at = time.time()
        for name, lim in cfg.models.items():
            self._add_model(name, lim.rpm, lim.tpm)

    # ----------------------------------------------------------------- setup
    def _add_model(self, name: str, rpm: int, tpm: int) -> ModelRuntime:
        rt = ModelRuntime(name, SlotLedger(rpm, tpm, self.cfg.defaults.guard_fraction),
                          self.cfg.defaults.deadline_s)
        self.models[name] = rt
        self.limit_events.append({"at": time.time(), "model": name, "rpm": rpm, "tpm": tpm})
        return rt

    async def start(self) -> None:
        conn = aiohttp.TCPConnector(limit=self.max_concurrency, limit_per_host=self.max_concurrency,
                                    ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=30))
        self._tasks.append(asyncio.create_task(self._sampler()))
        for rt in self.models.values():
            self._start_scheduler(rt)

    def _start_scheduler(self, rt: ModelRuntime) -> None:
        rt.wake = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._scheduler(rt)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------- admission
    def _new_id(self) -> str:
        return f"r-{uuid.uuid4().hex[:12]}"

    def estimate_wait(self, rt: ModelRuntime, tokens: int) -> float:
        """Projected seconds until a request appended now would be sent."""
        led = rt.ledger
        base = led.seconds_until_fits(1, tokens)
        if math.isinf(base):
            return math.inf
        rps = led.eff_rpm / 60.0
        tps = led.eff_tpm / 60.0
        ahead = len(rt.queue) + len(rt.retry)
        queued = max(ahead / rps if rps > 0 else math.inf,
                     (rt.queued_tokens + tokens) / tps if tps > 0 else math.inf)
        return base + queued

    def submit(self, model: str, est_tokens: int, payload: Any = None, client_id: str | None = None,
               deadline_s: float | None = None, intended_at: float | None = None,
               batch_id: str | None = None, rid: str | None = None) -> Request:
        rt = self.models.get(model)
        if rt is None:
            raise KeyError(model)
        now = time.time()
        deadline = now + (deadline_s if deadline_s is not None else rt.deadline_s)
        req = Request(rid or self._new_id(), client_id, model, int(est_tokens), payload, batch_id,
                      deadline, intended_at, now)
        self.requests[req.id] = req
        rt.counts["submitted"] += 1
        wait = self.estimate_wait(rt, req.est_tokens)
        if now + wait > deadline:
            req.reason = ("exceeds_model_limit" if math.isinf(wait)
                          else "no_capacity_before_deadline")
            req.eta = None
            self._finalize(rt, req, "rejected", count_from=None)
            req.error = {"retry_after_s": None if math.isinf(wait) else round(wait, 2)}
            return req
        req.eta = now + wait
        rt.counts["waiting"] += 1
        rt.queue.append(req)
        rt.queued_tokens += req.est_tokens
        return req

    # -------------------------------------------------------------- dispatch
    async def _scheduler(self, rt: ModelRuntime) -> None:
        led = rt.ledger
        while True:
            try:
                now = time.time()
                sent_any = False
                # 1) retries whose backoff elapsed go first
                while rt.retry and rt.retry[0].not_before <= now:
                    req = rt.retry[0]
                    if now > req.deadline:
                        rt.retry.popleft()
                        rt.queued_tokens -= req.est_tokens
                        req.reason = "deadline_passed_during_retry"
                        self._finalize(rt, req, "failed", count_from="waiting")
                        continue
                    if self._inflight >= self.max_concurrency or not led.can_send(1, req.est_tokens):
                        break
                    rt.retry.popleft()
                    self._send(rt, req)
                    sent_any = True
                # 2) fresh requests in FIFO order
                while rt.queue:
                    req = rt.queue[0]
                    if now > req.deadline:
                        rt.queue.popleft()
                        rt.queued_tokens -= req.est_tokens
                        req.reason = "deadline_passed_before_send"
                        self._finalize(rt, req, "expired", count_from="waiting")
                        continue
                    if self._inflight >= self.max_concurrency or not led.can_send(1, req.est_tokens):
                        break
                    rt.queue.popleft()
                    self._send(rt, req)
                    sent_any = True
                if not sent_any and not rt.queue and not rt.retry:
                    rt.wake.clear()
                    try:
                        await asyncio.wait_for(rt.wake.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(TICK_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let a scheduler die
                print(f"[scheduler {rt.name}] error: {e!r}")
                await asyncio.sleep(0.05)

    def _send(self, rt: ModelRuntime, req: Request) -> None:
        rt.queued_tokens -= req.est_tokens
        req.bucket = rt.ledger.record(1, req.est_tokens)
        req.attempts += 1
        req.sent_at = time.time()
        req.state = "in_flight"
        rt.counts["waiting"] -= 1
        rt.counts["in_flight"] += 1
        rt.sec["sent"] += 1
        rt.sec["sent_tokens"] += req.est_tokens
        self._inflight += 1
        asyncio.create_task(self._call_provider(rt, req))

    async def _call_provider(self, rt: ModelRuntime, req: Request) -> None:
        status, body = 0, None
        try:
            async with self._session.post(
                f"{self.provider_url}/v1/infer",
                json={"request_id": req.id, "model": req.model, "estimated_tokens": req.est_tokens,
                      "payload": req.payload, "attempt": req.attempts},
            ) as resp:
                status = resp.status
                body = await resp.json(content_type=None)
        except Exception as e:  # network error -> transient
            status, body = 0, {"error": f"network: {e.__class__.__name__}"}
        finally:
            self._inflight -= 1

        actual = req.est_tokens
        if status == 200:
            actual = int(body.get("usage", {}).get("actual_tokens", req.est_tokens))
        if req.bucket is not None:
            rt.ledger.reconcile(req.bucket, req.est_tokens, actual)

        if status == 200:
            req.output = body.get("output")
            req.actual_tokens = actual
            self._finalize(rt, req, "succeeded", count_from="in_flight")
            return
        if status == 400:
            req.error = body
            req.reason = "permanent_provider_error"
            self._finalize(rt, req, "failed", count_from="in_flight")
            return
        # transient (503), rate limited (429) or network error -> retry with backoff
        if status == 429:
            rt.sec["rate_limited"] += 1
        req.error = body
        backoff = min(0.2 * (2 ** (req.attempts - 1)), 5.0) * self._rand.uniform(0.8, 1.2)
        if req.attempts >= self.cfg.defaults.max_attempts or time.time() + backoff > req.deadline:
            req.reason = "attempts_exhausted" if req.attempts >= self.cfg.defaults.max_attempts \
                else "deadline_passed_during_retry"
            self._finalize(rt, req, "failed", count_from="in_flight")
            return
        rt.sec["retries"] += 1
        req.state = "waiting"
        req.not_before = time.time() + backoff
        rt.counts["in_flight"] -= 1
        rt.counts["waiting"] += 1
        rt.queued_tokens += req.est_tokens
        rt.retry.append(req)
        if rt.wake:
            rt.wake.set()

    def _finalize(self, rt: ModelRuntime, req: Request, state: str, count_from: str | None) -> None:
        if count_from:
            rt.counts[count_from] -= 1
        req.state = state
        req.finished_at = time.time()
        rt.counts[state] += 1
        if state in ("succeeded", "failed"):
            rt.sec["completed"] += 1
            rt.latencies_ms.append((req.finished_at - req.intended_at) * 1000.0)
        rt.sec[state] = rt.sec.get(state, 0) + 1
        if self.on_final is not None and req.batch_id is not None:
            self.on_final(req)

    # ------------------------------------------------------------ limits
    def set_limits(self, model: str, rpm: int, tpm: int) -> dict:
        rt = self.models.get(model)
        if rt is None:
            rt = self._add_model(model, rpm, tpm)
            self._start_scheduler(rt)
            return {"model": model, "rpm": rpm, "tpm": tpm, "expired": 0}
        rt.ledger.set_limits(rpm, tpm)
        self.limit_events.append({"at": time.time(), "model": model, "rpm": rpm, "tpm": tpm})
        expired = self._reproject(rt)
        rt.wake.set()
        return {"model": model, "rpm": rpm, "tpm": tpm, "expired": expired}

    def _reproject(self, rt: ModelRuntime) -> int:
        """Recompute every waiting request's ETA under the new limits; expire the doomed."""
        led = rt.ledger
        now = time.time()
        base = led.seconds_until_fits(1, 1)
        rps = led.eff_rpm / 60.0
        tps = led.eff_tpm / 60.0
        kept: deque = deque()
        tokens_ahead = 0
        expired = 0
        for i, req in enumerate(rt.queue):
            if math.isinf(base) or rps <= 0:
                wait = math.inf
            else:
                wait = base + max((len(rt.retry) + len(kept)) / rps,
                                  (tokens_ahead + req.est_tokens) / tps)
            if now + wait > req.deadline:
                req.reason = "limit_reduced_slot_past_deadline"
                rt.queued_tokens -= req.est_tokens
                self._finalize(rt, req, "expired", count_from="waiting")
                expired += 1
                continue
            req.eta = now + wait
            tokens_ahead += req.est_tokens
            kept.append(req)
        rt.queue = kept
        return expired

    # ------------------------------------------------------------ metrics
    async def _sampler(self) -> None:
        while True:
            await asyncio.sleep(1.0 - (time.time() % 1.0))
            now = time.time()
            for rt in self.models.values():
                req_used, tok_used = rt.ledger.usage()
                self.timeseries.append({
                    "t": round(now, 3), "model": rt.name,
                    "rpm_limit": rt.ledger.rpm, "tpm_limit": rt.ledger.tpm,
                    "window_requests": req_used, "window_tokens": tok_used,
                    "waiting": rt.counts["waiting"], "in_flight": rt.counts["in_flight"],
                    **rt.sec,
                })
                for k in rt.sec:
                    rt.sec[k] = 0
            self._evict(now)

    def _evict(self, now: float, keep_s: float = 900.0) -> None:
        # Drop finished stand-alone requests after keep_s to bound memory on long runs.
        if len(self.requests) < 500_000:
            return
        cutoff = now - keep_s
        for rid in [rid for rid, r in self.requests.items()
                    if r.state in FINAL_STATES and r.batch_id is None and (r.finished_at or now) < cutoff]:
            del self.requests[rid]

    def totals(self) -> dict:
        per_model = {}
        agg = {k: 0 for k in ("submitted",) + STATES}
        for name, rt in self.models.items():
            c = dict(rt.counts)
            c["completed"] = c["succeeded"] + c["failed"]
            c["invariant_ok"] = c["submitted"] == sum(c[s] for s in STATES)
            c["rpm_limit"], c["tpm_limit"] = rt.ledger.rpm, rt.ledger.tpm
            c["window_requests"], c["window_tokens"] = rt.ledger.usage()
            per_model[name] = c
            for k in agg:
                agg[k] += rt.counts[k]
        agg["completed"] = agg["succeeded"] + agg["failed"]
        agg["invariant_ok"] = agg["submitted"] == sum(agg[s] for s in STATES)
        return {"totals": agg, "models": per_model}

    def latency_stats(self) -> dict:
        import numpy as np

        out = {}
        allv = []
        for name, rt in self.models.items():
            v = rt.latencies_ms
            allv.extend(v)
            out[name] = _pcts(np, v)
        out["_all"] = _pcts(np, allv)
        return out

    def reset(self) -> None:
        """Clear counters and history for a fresh run (limits and queues stay)."""
        for rt in self.models.values():
            for k in rt.counts:
                rt.counts[k] = 0
            rt.counts["waiting"] = len(rt.queue) + len(rt.retry)
            rt.counts["submitted"] = rt.counts["waiting"]
            rt.latencies_ms.clear()
        self.requests = {rid: r for rid, r in self.requests.items() if r.state not in FINAL_STATES}
        for rt in self.models.values():
            rt.counts["in_flight"] = sum(1 for r in self.requests.values()
                                         if r.model == rt.name and r.state == "in_flight")
            rt.counts["submitted"] += rt.counts["in_flight"]
        self.timeseries.clear()
        self.limit_events = [{"at": time.time(), "model": n, "rpm": rt.ledger.rpm, "tpm": rt.ledger.tpm}
                             for n, rt in self.models.items()]
        self.started_at = time.time()


def _pcts(np, values) -> dict:
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    a = np.asarray(values)
    p50, p95, p99 = np.percentile(a, [50, 95, 99])
    return {"count": int(a.size), "p50_ms": round(float(p50), 2), "p95_ms": round(float(p95), 2),
            "p99_ms": round(float(p99), 2), "max_ms": round(float(a.max()), 2),
            "mean_ms": round(float(a.mean()), 2)}
