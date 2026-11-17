"""Mock callback receiver (the client's webhook endpoint).

Rejects the first N delivery attempts per batch (HTTP 503) to simulate a
destination outage, then accepts and stores the body.

Run:  python -m tools.mock_receiver --port 9100 --reject-first 2
"""
from __future__ import annotations

import argparse
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def create_app(reject_first: int = 2) -> FastAPI:
    app = FastAPI(title="Mock callback receiver")
    state = {"reject_first": reject_first, "attempts": {}, "delivered": {}, "log": []}

    @app.post("/callback")
    async def callback(request: Request):
        key = request.headers.get("Idempotency-Key", "unknown")
        raw = await request.body()
        n = state["attempts"].get(key, 0) + 1
        state["attempts"][key] = n
        accepted = n > state["reject_first"]
        state["log"].append({"batch_id": key, "attempt": n, "at": time.time(),
                             "header_attempt": request.headers.get("X-Inferq-Attempt"),
                             "accepted": accepted})
        if not accepted:
            return JSONResponse({"error": "simulated outage", "attempt": n}, status_code=503)
        if key in state["delivered"]:
            # idempotent: duplicate delivery of the same batch is acknowledged but not re-applied
            return {"ok": True, "duplicate": True}
        state["delivered"][key] = {"received_at": time.time(), "attempt": n, "body": json.loads(raw)}
        return {"ok": True}

    @app.get("/received")
    async def received():
        return {"delivered": state["delivered"], "log": state["log"]}

    @app.put("/config")
    async def config(reject_first: int):
        state["reject_first"] = reject_first
        return {"reject_first": reject_first}

    @app.post("/reset")
    async def reset():
        state["attempts"].clear()
        state["delivered"].clear()
        state["log"].clear()
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--reject-first", type=int, default=2)
    a = ap.parse_args()
    uvicorn.run(create_app(a.reject_first), host=a.host, port=a.port, log_level="warning", backlog=8192, access_log=False)


if __name__ == "__main__":
    main()
