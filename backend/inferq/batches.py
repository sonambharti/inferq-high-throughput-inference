"""Asynchronous batches, sealed statements and the durable callback outbox.

* A batch is acknowledged immediately; its requests are admitted in the background.
* Every final transition decrements `remaining`.  The transition that reaches 0
  SEALS the batch exactly once: an immutable summary (+ results) is written to
  SQLite and, if a callback_url was given, an outbox row is created.
* GET /v1/batches/{id} and the callback body are produced from the same sealed
  summary, so they cannot disagree.
* The outbox deliverer retries with exponential backoff + jitter, sends the
  batch id as Idempotency-Key, records every attempt, and survives restarts
  (pending rows are reloaded on startup).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, created_at REAL, total INTEGER, callback_url TEXT,
  status TEXT, sealed_at REAL, summary TEXT
);
CREATE TABLE IF NOT EXISTS batch_results (
  batch_id TEXT, seq INTEGER, request_id TEXT, data TEXT,
  PRIMARY KEY (batch_id, seq)
);
CREATE TABLE IF NOT EXISTS outbox (
  batch_id TEXT PRIMARY KEY, url TEXT, body TEXT, status TEXT, attempts INTEGER,
  next_attempt_at REAL, delivered_at REAL, history TEXT
);
"""


@dataclass
class LiveBatch:
    id: str
    created_at: float
    total: int
    callback_url: str | None
    remaining: int
    request_ids: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {"succeeded": 0, "failed": 0, "expired": 0, "rejected": 0})
    sealed: bool = False
    admitted: int = 0


class BatchManager:
    def __init__(self, db_path: str, engine, public_base_url: str = "http://127.0.0.1:8000",
                 backoff_base_s: float = 1.0, backoff_max_s: float = 60.0, max_attempts: int = 12):
        self.engine = engine
        self.public_base_url = public_base_url.rstrip("/")
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.max_attempts = max_attempts
        self.live: dict[str, LiveBatch] = {}
        self._lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        # Request-level state is in memory; batches unsealed at shutdown cannot complete.
        self._sql("UPDATE batches SET status='interrupted' WHERE status='processing'")
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self._task = asyncio.create_task(self._deliverer())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    def _sql(self, q: str, args: tuple = (), many: bool = False):
        with self._lock:
            if many:
                # one transaction for the whole batch (autocommit would fsync per row)
                self.db.execute("BEGIN")
                try:
                    self.db.executemany(q, args)
                    self.db.execute("COMMIT")
                except Exception:
                    self.db.execute("ROLLBACK")
                    raise
                return None
            return self.db.execute(q, args).fetchall()

    # ----------------------------------------------------------------- create
    def create(self, items: list[dict], callback_url: str | None, deadline_s: float | None) -> LiveBatch:
        bid = f"b-{uuid.uuid4().hex[:12]}"
        now = time.time()
        b = LiveBatch(bid, now, len(items), callback_url, remaining=len(items))
        self.live[bid] = b
        self._sql("INSERT INTO batches (id, created_at, total, callback_url, status) VALUES (?,?,?,?,?)",
                  (bid, now, len(items), callback_url, "processing"))
        asyncio.get_running_loop().create_task(self._admit(b, items, deadline_s))
        return b

    async def _admit(self, b: LiveBatch, items: list[dict], deadline_s: float | None) -> None:
        # Admit in chunks so the event loop keeps serving other requests.
        for i, it in enumerate(items):
            # register the id BEFORE submitting: a request can reach a final state
            # synchronously (rejected at admission) and may be the one that seals the batch
            rid = f"r-{uuid.uuid4().hex[:12]}"
            b.request_ids.append(rid)
            try:
                self.engine.submit(
                    model=it["model"], est_tokens=it.get("estimated_tokens", 1000),
                    payload=it.get("payload"), client_id=it.get("request_id"),
                    deadline_s=it.get("deadline_s", deadline_s), intended_at=it.get("intended_at"),
                    batch_id=b.id, rid=rid)
            except KeyError:
                # unknown model: account for it as rejected so the batch still seals
                self._record_unknown(b, rid, it)
            b.admitted += 1
            if i % 2000 == 1999:
                await asyncio.sleep(0)

    def _record_unknown(self, b: LiveBatch, rid: str, it: dict) -> None:
        from inferq.engine import Request
        r = Request(rid, it.get("request_id"), it.get("model"), int(it.get("estimated_tokens", 0)),
                    None, b.id, time.time(), None, time.time())
        r.state, r.reason, r.finished_at = "rejected", "unknown_model", time.time()
        self.engine.requests[rid] = r
        self.on_final(r)

    # --------------------------------------------------------- final callback
    def on_final(self, req) -> None:
        b = self.live.get(req.batch_id)
        if b is None or b.sealed:
            return
        b.counts[req.state] = b.counts.get(req.state, 0) + 1
        b.remaining -= 1
        if b.remaining == 0:
            self._seal(b)

    def _seal(self, b: LiveBatch) -> None:
        b.sealed = True
        sealed_at = time.time()
        results = []
        for seq, rid in enumerate(b.request_ids):
            r = self.engine.requests.get(rid)
            results.append((b.id, seq, rid, json.dumps(r.to_dict() if r else {"request_id": rid})))
        c = b.counts
        status = "completed" if c["failed"] + c["expired"] + c["rejected"] == 0 else "completed_with_errors"
        summary = {
            "batch_id": b.id,
            "status": status,
            "total": b.total,
            "successful": c["succeeded"],
            "failed": c["failed"] + c["expired"] + c["rejected"],
            "counts": {"succeeded": c["succeeded"], "failed": c["failed"], "expired": c["expired"],
                       "rejected": c["rejected"]},
            "created_at": b.created_at,
            "sealed_at": sealed_at,
            "duration_s": round(sealed_at - b.created_at, 3),
            "results_url": f"{self.public_base_url}/v1/batches/{b.id}/results",
        }
        summary["summary_sha256"] = hashlib.sha256(
            json.dumps(summary, sort_keys=True).encode()).hexdigest()
        body = json.dumps(summary, sort_keys=True)
        # results + sealed summary + outbox row commit atomically: a crash can never leave
        # a sealed batch without its pending callback (or a callback without its results)
        with self._lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany("INSERT OR REPLACE INTO batch_results (batch_id, seq, request_id, data) "
                                    "VALUES (?,?,?,?)", results)
                self.db.execute("UPDATE batches SET status=?, sealed_at=?, summary=? WHERE id=?",
                                (status, sealed_at, body, b.id))
                if b.callback_url:
                    self.db.execute("INSERT OR REPLACE INTO outbox (batch_id, url, body, status, attempts, "
                                    "next_attempt_at, history) VALUES (?,?,?,?,?,?,?)",
                                    (b.id, b.callback_url, body, "pending", 0, sealed_at, "[]"))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if b.callback_url:
            self._wake.set()
        # live batch no longer needed: everything is in SQLite
        self.live.pop(b.id, None)

    # ----------------------------------------------------------------- reads
    def get(self, bid: str) -> dict | None:
        rows = self._sql("SELECT id, created_at, total, callback_url, status, sealed_at, summary "
                         "FROM batches WHERE id=?", (bid,))
        if not rows:
            return None
        _, created_at, total, cb, status, sealed_at, summary = rows[0]
        if summary:
            out = json.loads(summary)
        else:
            b = self.live.get(bid)
            c = b.counts if b else {"succeeded": 0, "failed": 0, "expired": 0, "rejected": 0}
            out = {"batch_id": bid, "status": status, "total": total, "successful": c["succeeded"],
                   "failed": c["failed"] + c["expired"] + c["rejected"], "counts": dict(c),
                   "created_at": created_at, "sealed_at": None, "duration_s": None,
                   "results_url": f"{self.public_base_url}/v1/batches/{bid}/results",
                   "remaining": b.remaining if b else None}
        out["callback"] = self.callback_state(bid) if cb else None
        return out

    def callback_state(self, bid: str) -> dict | None:
        rows = self._sql("SELECT url, status, attempts, next_attempt_at, delivered_at, history "
                         "FROM outbox WHERE batch_id=?", (bid,))
        if not rows:
            return {"status": "awaiting_seal", "attempts": 0, "history": []}
        url, status, attempts, nxt, delivered, hist = rows[0]
        return {"url": url, "status": status, "attempts": attempts, "next_attempt_at": nxt,
                "delivered_at": delivered, "history": json.loads(hist)}

    def results(self, bid: str, offset: int, limit: int) -> list[dict]:
        rows = self._sql("SELECT data FROM batch_results WHERE batch_id=? ORDER BY seq LIMIT ? OFFSET ?",
                         (bid, limit, offset))
        return [json.loads(r[0]) for r in rows]

    def list(self, limit: int = 50) -> list[dict]:
        rows = self._sql("SELECT id FROM batches ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.get(r[0]) for r in rows]

    # -------------------------------------------------------------- delivery
    async def _deliverer(self) -> None:
        while True:
            try:
                now = time.time()
                due = self._sql("SELECT batch_id, url, body, attempts, history FROM outbox "
                                "WHERE status='pending' AND next_attempt_at<=? ORDER BY next_attempt_at "
                                "LIMIT 50", (now,))
                for bid, url, body, attempts, hist in due:
                    await self._attempt(bid, url, body, attempts, json.loads(hist))
                nxt = self._sql("SELECT MIN(next_attempt_at) FROM outbox WHERE status='pending'")
                sleep = 1.0
                if nxt and nxt[0][0] is not None:
                    sleep = max(0.05, min(1.0, nxt[0][0] - time.time()))
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=sleep)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[callback deliverer] error: {e!r}")
                await asyncio.sleep(1.0)

    async def _attempt(self, bid: str, url: str, body: str, attempts: int, hist: list) -> None:
        attempt = attempts + 1
        started = time.time()
        code, err = None, None
        try:
            async with self._session.post(url, data=body, headers={
                "Content-Type": "application/json",
                "Idempotency-Key": bid,
                "X-Inferq-Batch-Id": bid,
                "X-Inferq-Attempt": str(attempt),
            }) as resp:
                code = resp.status
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
        ok = code is not None and 200 <= code < 300
        hist.append({"attempt": attempt, "at": started, "status_code": code, "error": err,
                     "elapsed_ms": round((time.time() - started) * 1000, 1)})
        if ok:
            self._sql("UPDATE outbox SET status='delivered', attempts=?, delivered_at=?, history=? WHERE batch_id=?",
                      (attempt, time.time(), json.dumps(hist), bid))
            return
        if attempt >= self.max_attempts:
            self._sql("UPDATE outbox SET status='failed', attempts=?, history=? WHERE batch_id=?",
                      (attempt, json.dumps(hist), bid))
            return
        delay = min(self.backoff_base_s * (2 ** (attempt - 1)), self.backoff_max_s) * random.uniform(0.8, 1.2)
        self._sql("UPDATE outbox SET attempts=?, next_attempt_at=?, history=? WHERE batch_id=?",
                  (attempt, time.time() + delay, json.dumps(hist), bid))
