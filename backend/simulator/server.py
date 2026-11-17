"""Provider simulator (HTTP).

Behaves like an external inference provider:
  * enforces its OWN copy of per-model RPM/TPM limits over a strict rolling
    60 s window (600 x 100 ms buckets) and answers 429 when exceeded;
  * simulates latency and success / transient / permanent outcomes;
  * records every accepted arrival in 10 ms buckets so the independent
    auditor can recompute peak rolling-60s usage without trusting the
    dispatcher.

This module deliberately does NOT import inferq.ledger: the provider's limit
enforcement and the auditor's evidence must be independent of the system
under test.

Run:  python -m simulator.server --port 9000
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections import deque

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from inferq.config import SimModelConfig, load_config
from simulator.core import PERMANENT, SUCCESS, TRANSIENT, sample_one

AUDIT_BUCKET_S = 0.01  # 10 ms evidence resolution


class InferBody(BaseModel):
    request_id: str
    model: str
    estimated_tokens: int
    payload: dict | str | None = None
    attempt: int = 1


class LimitsBody(BaseModel):
    rpm: int
    tpm: int


class BehaviorBody(BaseModel):
    latency_ms: float | None = None
    jitter_ms: float | None = None
    tail_prob: float | None = None
    tail_ms: float | None = None
    p_transient: float | None = None
    p_permanent: float | None = None
    token_noise: list[float] | None = None


class ProviderWindow:
    """Simple deque-of-buckets rolling window (independent of inferq.ledger)."""

    def __init__(self, rpm: int, tpm: int):
        self.rpm, self.tpm = rpm, tpm
        self.buckets: deque[list] = deque()  # [bucket_id, count, tokens]
        self.count = 0
        self.tokens = 0

    def _trim(self, now_bucket: int) -> None:
        while self.buckets and self.buckets[0][0] <= now_bucket - 600:
            _, c, t = self.buckets.popleft()
            self.count -= c
            self.tokens -= t

    def try_admit(self, tokens: int, now: float) -> bool:
        b = int(now * 10)
        self._trim(b)
        if self.count + 1 > self.rpm or self.tokens + tokens > self.tpm:
            return False
        if self.buckets and self.buckets[-1][0] == b:
            self.buckets[-1][1] += 1
            self.buckets[-1][2] += tokens
        else:
            self.buckets.append([b, 1, tokens])
        self.count += 1
        self.tokens += tokens
        return True


class ModelState:
    def __init__(self, name: str, rpm: int, tpm: int, behavior: SimModelConfig):
        self.name = name
        self.window = ProviderWindow(rpm, tpm)
        self.behavior = behavior
        self.limit_history: list[dict] = [{"at": time.time(), "rpm": rpm, "tpm": tpm}]
        # audit evidence: 10 ms bucket -> [accepted_count, actual_tokens]
        self.arrivals: dict[int, list[int]] = {}
        self.counters = {"accepted": 0, "rate_limited": 0, "success": 0, "transient_error": 0,
                         "permanent_error": 0}


def create_app(config_path: str | None = None, seed: int | None = None) -> FastAPI:
    cfg = load_config(config_path)
    rng = random.Random(seed)
    models: dict[str, ModelState] = {
        name: ModelState(name, lim.rpm, lim.tpm, cfg.sim_for(name)) for name, lim in cfg.models.items()
    }
    app = FastAPI(title="Provider Simulator")

    def get_model(name: str) -> ModelState:
        m = models.get(name)
        if m is None:
            raise HTTPException(404, f"unknown model {name}")
        return m

    @app.post("/v1/infer")
    async def infer(body: InferBody):
        m = get_model(body.model)
        now = time.time()
        outcome, latency, actual = sample_one(m.behavior, body.estimated_tokens, rng)
        # The provider meters the tokens the request actually consumed (known to the
        # simulator at arrival).  The dispatcher reserves the estimate and reconciles.
        if not m.window.try_admit(actual, now):
            m.counters["rate_limited"] += 1
            return JSONResponse({"error": "rate_limited", "request_id": body.request_id}, status_code=429)
        m.counters["accepted"] += 1
        b = int(now / AUDIT_BUCKET_S)
        slot = m.arrivals.get(b)
        if slot is None:
            m.arrivals[b] = [1, actual]
        else:
            slot[0] += 1
            slot[1] += actual
        await asyncio.sleep(latency)
        if outcome == TRANSIENT:
            m.counters["transient_error"] += 1
            return JSONResponse({"error": "transient", "request_id": body.request_id}, status_code=503)
        if outcome == PERMANENT:
            m.counters["permanent_error"] += 1
            return JSONResponse({"error": "invalid_request", "request_id": body.request_id}, status_code=400)
        m.counters["success"] += 1
        return {
            "request_id": body.request_id,
            "model": body.model,
            "output": f"simulated completion for {body.request_id}",
            "usage": {"estimated_tokens": body.estimated_tokens, "actual_tokens": actual},
            "latency_ms": round(latency * 1000, 2),
        }

    @app.get("/v1/models")
    async def list_models():
        return {
            name: {"rpm": m.window.rpm, "tpm": m.window.tpm, "behavior": m.behavior.__dict__,
                   "counters": m.counters}
            for name, m in models.items()
        }

    @app.put("/v1/models/{name}/limits")
    async def set_limits(name: str, body: LimitsBody):
        m = models.get(name)
        if m is None:
            m = models[name] = ModelState(name, body.rpm, body.tpm, cfg.sim_for(name))
        m.window.rpm, m.window.tpm = body.rpm, body.tpm
        m.limit_history.append({"at": time.time(), "rpm": body.rpm, "tpm": body.tpm})
        return {"model": name, "rpm": body.rpm, "tpm": body.tpm}

    @app.put("/v1/models/{name}/behavior")
    async def set_behavior(name: str, body: BehaviorBody):
        m = get_model(name)
        m.behavior = m.behavior.merged({k: v for k, v in body.model_dump().items() if v is not None})
        return {"model": name, "behavior": m.behavior.__dict__}

    @app.get("/v1/audit/{name}")
    async def audit(name: str):
        m = get_model(name)
        items = sorted(m.arrivals.items())
        return {
            "model": name,
            "bucket_s": AUDIT_BUCKET_S,
            "buckets": [k for k, _ in items],
            "counts": [v[0] for _, v in items],
            "tokens": [v[1] for _, v in items],
            "limit_history": m.limit_history,
            "counters": m.counters,
        }

    @app.post("/v1/admin/reset")
    async def reset():
        for m in models.values():
            m.arrivals.clear()
            for k in m.counters:
                m.counters[k] = 0
            m.limit_history = [{"at": time.time(), "rpm": m.window.rpm, "tpm": m.window.tpm}]
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Provider simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--config", default=None)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    uvicorn.run(create_app(args.config, args.seed), host=args.host, port=args.port, log_level="warning", backlog=8192,
                access_log=False)


if __name__ == "__main__":
    main()
