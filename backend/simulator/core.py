"""Provider behaviour model shared by the HTTP simulator and the in-process benchmark.

Outcome codes:
  0 = success, 1 = transient failure (retryable, HTTP 503), 2 = permanent failure (HTTP 400)
"""
from __future__ import annotations

import random

import numpy as np

from inferq.config import SimModelConfig

SUCCESS, TRANSIENT, PERMANENT = 0, 1, 2
OUTCOME_NAMES = {SUCCESS: "success", TRANSIENT: "transient_error", PERMANENT: "permanent_error"}


def sample_one(cfg: SimModelConfig, est_tokens: int, rng: random.Random) -> tuple[int, float, int]:
    """Return (outcome, latency_seconds, actual_tokens) for one request."""
    r = rng.random()
    if r < cfg.p_permanent:
        outcome = PERMANENT
    elif r < cfg.p_permanent + cfg.p_transient:
        outcome = TRANSIENT
    else:
        outcome = SUCCESS
    lat = cfg.latency_ms + rng.uniform(-cfg.jitter_ms, cfg.jitter_ms)
    if rng.random() < cfg.tail_prob:
        lat += rng.uniform(0, cfg.tail_ms)
    lat = max(1.0, lat)
    lo, hi = cfg.token_noise
    actual = max(1, int(est_tokens * rng.uniform(lo, hi))) if outcome == SUCCESS else int(est_tokens)
    return outcome, lat / 1000.0, actual


def sample_many(cfg: SimModelConfig, est_tokens: np.ndarray, rng: np.random.Generator):
    """Vectorised version: returns (outcome[int8], latency_ms[float32], actual_tokens[int64])."""
    n = est_tokens.shape[0]
    r = rng.random(n)
    outcome = np.zeros(n, dtype=np.int8)
    outcome[r < cfg.p_permanent + cfg.p_transient] = TRANSIENT
    outcome[r < cfg.p_permanent] = PERMANENT
    lat = cfg.latency_ms + rng.uniform(-cfg.jitter_ms, cfg.jitter_ms, n)
    if cfg.tail_prob > 0:
        tail = rng.random(n) < cfg.tail_prob
        lat[tail] += rng.uniform(0, cfg.tail_ms, int(tail.sum()))
    np.maximum(lat, 1.0, out=lat)
    lo, hi = cfg.token_noise
    actual = np.maximum(1, (est_tokens * rng.uniform(lo, hi, n)).astype(np.int64))
    return outcome, lat.astype(np.float32), actual
