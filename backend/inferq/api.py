"""Control plane HTTP API.

Run:  python -m inferq.api --port 8000 --provider-url http://127.0.0.1:9000
"""
from __future__ import annotations

import argparse
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from inferq.batches import BatchManager
from inferq.config import load_config
from inferq.engine import Engine


class RequestBody(BaseModel):
    model: str
    estimated_tokens: int = Field(ge=1)
    payload: Any = None
    request_id: str | None = None          # client-supplied id (echoed back)
    deadline_s: float | None = Field(default=None, gt=0)
    intended_at: float | None = None       # load generator's intended send time (latency origin)


class BatchBody(BaseModel):
    requests: list[RequestBody] = Field(min_length=1, max_length=100_000)
    callback_url: str | None = None
    deadline_s: float | None = Field(default=None, gt=0)


class LimitsBody(BaseModel):
    rpm: int = Field(ge=0)
    tpm: int = Field(ge=0)
    propagate_to_provider: bool = True     # simulation convenience: keep the simulator in sync


def create_app(config_path: str | None = None, provider_url: str = "http://127.0.0.1:9000",
               db_path: str = "inferq.db", public_base_url: str = "http://127.0.0.1:8000",
               callback_backoff_s: float = 1.0) -> FastAPI:
    cfg = load_config(config_path)
    engine = Engine(cfg, provider_url)
    batches = BatchManager(db_path, engine, public_base_url, backoff_base_s=callback_backoff_s)
    engine.on_final = batches.on_final

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.start()
        await batches.start()
        yield
        await batches.stop()
        await engine.stop()

    app = FastAPI(title="inferq control plane", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.engine = engine
    app.state.batches = batches

    # ------------------------------------------------------------- requests
    @app.post("/v1/requests", status_code=202)
    async def submit(body: RequestBody):
        try:
            req = engine.submit(body.model, body.estimated_tokens, body.payload, body.request_id,
                                body.deadline_s, body.intended_at)
        except KeyError:
            raise HTTPException(404, f"unknown model '{body.model}'")
        out = {"request_id": req.id, "client_request_id": req.client_id, "state": req.state,
               "eta_s": round(req.eta - time.time(), 3) if req.eta else None,
               "status_url": f"/v1/requests/{req.id}"}
        if req.state == "rejected":
            retry_after = (req.error or {}).get("retry_after_s")
            out.update(reason=req.reason, retry_after_s=retry_after)
            headers = {"Retry-After": str(max(1, int(retry_after)))} if retry_after else {}
            return JSONResponse(out, status_code=429, headers=headers)
        return out

    @app.get("/v1/requests/{rid}")
    async def get_request(rid: str):
        r = engine.requests.get(rid)
        if r is None:
            raise HTTPException(404, "unknown request id")
        return r.to_dict()

    # -------------------------------------------------------------- batches
    @app.post("/v1/batches", status_code=202)
    async def create_batch(body: BatchBody):
        items = [r.model_dump() for r in body.requests]
        b = batches.create(items, body.callback_url, body.deadline_s)
        return {"batch_id": b.id, "status": "processing", "total": b.total,
                "status_url": f"/v1/batches/{b.id}", "results_url": f"/v1/batches/{b.id}/results",
                "accepted_at": b.created_at}

    @app.get("/v1/batches")
    async def list_batches(limit: int = Query(50, le=500)):
        return {"batches": batches.list(limit)}

    @app.get("/v1/batches/{bid}")
    async def get_batch(bid: str):
        b = batches.get(bid)
        if b is None:
            raise HTTPException(404, "unknown batch id")
        return b

    @app.get("/v1/batches/{bid}/results")
    async def batch_results(bid: str, offset: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=20000)):
        b = batches.get(bid)
        if b is None:
            raise HTTPException(404, "unknown batch id")
        if b.get("sealed_at") is None:
            return {"batch_id": bid, "status": b["status"], "results": [], "offset": offset,
                    "note": "results are available once the batch is sealed"}
        res = batches.results(bid, offset, limit)
        nxt = offset + len(res) if offset + len(res) < b["total"] else None
        return {"batch_id": bid, "total": b["total"], "offset": offset, "next_offset": nxt, "results": res}

    # --------------------------------------------------------------- models
    @app.get("/v1/models")
    async def models():
        return {name: {"rpm": rt.ledger.rpm, "tpm": rt.ledger.tpm,
                       "effective_rpm": rt.ledger.eff_rpm, "effective_tpm": rt.ledger.eff_tpm,
                       "deadline_s": rt.deadline_s}
                for name, rt in engine.models.items()}

    @app.put("/v1/models/{name}/limits")
    async def set_limits(name: str, body: LimitsBody):
        provider = None
        if body.propagate_to_provider:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.put(f"{provider_url}/v1/models/{name}/limits",
                                     json={"rpm": body.rpm, "tpm": body.tpm}) as resp:
                        provider = resp.status
            except Exception as e:
                provider = f"error: {e.__class__.__name__}"
        res = engine.set_limits(name, body.rpm, body.tpm)
        res["provider_update"] = provider
        return res

    # -------------------------------------------------------------- metrics
    @app.get("/v1/metrics")
    async def metrics():
        out = engine.totals()
        out["latency"] = engine.latency_stats()
        out["uptime_s"] = round(time.time() - engine.started_at, 1)
        out["in_flight_connections"] = engine._inflight
        return out

    @app.get("/v1/metrics/timeseries")
    async def timeseries(since: float = 0.0, model: str | None = None):
        pts = [p for p in engine.timeseries if p["t"] > since and (model is None or p["model"] == model)]
        return {"points": pts, "limit_events": engine.limit_events}

    @app.post("/v1/admin/reset")
    async def reset():
        engine.reset()
        return {"ok": True, "at": engine.started_at}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="inferq control plane")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--config", default=None)
    ap.add_argument("--provider-url", default=os.environ.get("PROVIDER_URL", "http://127.0.0.1:9000"))
    ap.add_argument("--db", default=os.environ.get("INFERQ_DB", "inferq.db"))
    ap.add_argument("--callback-backoff", type=float, default=1.0,
                    help="base seconds for callback retry backoff (doubles each attempt)")
    args = ap.parse_args()
    app = create_app(args.config, args.provider_url, args.db, f"http://{args.host}:{args.port}",
                     args.callback_backoff)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", backlog=8192, access_log=False)


if __name__ == "__main__":
    main()
