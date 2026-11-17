"""Independent limit auditor.

Reads ONLY the provider simulator's own arrival evidence (10 ms buckets of
accepted requests and metered tokens) plus the simulator's limit history, and
computes the peak usage over every rolling 60-second window.  It does not
import any of the dispatcher's limiter code.

Rule for windows that span a limit change: a window is checked against the
HIGHEST limit in force at any moment during that window (a lowered limit
cannot retroactively un-send requests already sent under the previous limit).

Usage:  python -m tools.auditor --simulator http://127.0.0.1:9000 --model model-a
"""
from __future__ import annotations

import argparse
import json
import urllib.request

import numpy as np

WINDOW_S = 60.0


def _limit_series(history: list[dict], t0: float, nb: int, bucket_s: float, key: str) -> np.ndarray:
    """Per-bucket limit in force (step function over the limit history)."""
    times = t0 + np.arange(nb) * bucket_s
    out = np.full(nb, history[0][key], dtype=np.float64)
    for ev in history:
        out[times >= ev["at"]] = ev[key]
    return out


def _sliding_max(a: np.ndarray, w: int) -> np.ndarray:
    """Max over every length-w window (O(n) via block decomposition)."""
    n = a.size
    if n <= w:
        return np.array([a.max()])
    pad = (-n) % w
    b = np.concatenate([a, np.full(pad, -np.inf)])
    blocks = b.reshape(-1, w)
    left = np.maximum.accumulate(blocks, axis=1).ravel()
    right = np.maximum.accumulate(blocks[:, ::-1], axis=1)[:, ::-1].ravel()
    m = n - w + 1
    return np.maximum(right[:m], left[w - 1:w - 1 + m])


def audit(data: dict, window_s: float = WINDOW_S) -> dict:
    bucket_s = float(data["bucket_s"])
    hist = data["limit_history"]
    if not data["buckets"]:
        return {"model": data["model"], "accepted": 0, "pass": True, "note": "no arrivals"}
    b = np.asarray(data["buckets"], dtype=np.int64)
    first, last = int(b.min()), int(b.max())
    nb = last - first + 1
    cnt = np.zeros(nb, dtype=np.int64)
    tok = np.zeros(nb, dtype=np.int64)
    np.add.at(cnt, b - first, np.asarray(data["counts"], dtype=np.int64))
    np.add.at(tok, b - first, np.asarray(data["tokens"], dtype=np.int64))
    w = int(round(window_s / bucket_s))
    t0 = first * bucket_s
    rpm_lim = _limit_series(hist, t0, nb, bucket_s, "rpm")
    tpm_lim = _limit_series(hist, t0, nb, bucket_s, "tpm")

    def windows(x):
        c = np.concatenate([[0], np.cumsum(x)])
        if nb <= w:
            return np.array([c[-1]])
        return c[w:] - c[:-w]

    req_win = windows(cnt)
    tok_win = windows(tok)
    rpm_max = _sliding_max(rpm_lim, min(w, nb))
    tpm_max = _sliding_max(tpm_lim, min(w, nb))
    req_ratio = req_win / np.maximum(rpm_max, 1)
    tok_ratio = tok_win / np.maximum(tpm_max, 1)
    ri, ti = int(np.argmax(req_ratio)), int(np.argmax(tok_ratio))
    viol_req = int((req_win > rpm_max).sum())
    viol_tok = int((tok_win > tpm_max).sum())

    # per-second observed series for charts
    per_s = max(1, int(round(1.0 / bucket_s)))
    ns = nb // per_s
    sec_cnt = cnt[: ns * per_s].reshape(ns, per_s).sum(axis=1)
    sec_tok = tok[: ns * per_s].reshape(ns, per_s).sum(axis=1)
    return {
        "model": data["model"],
        "accepted": int(cnt.sum()),
        "tokens": int(tok.sum()),
        "windows_checked": int(req_win.size),
        "window_s": window_s,
        "resolution_s": bucket_s,
        "peak_window_requests": int(req_win[ri]),
        "limit_rpm_at_peak": float(rpm_max[ri]),
        "peak_rpm_ratio": round(float(req_ratio[ri]), 5),
        "peak_window_tokens": int(tok_win[ti]),
        "limit_tpm_at_peak": float(tpm_max[ti]),
        "peak_tpm_ratio": round(float(tok_ratio[ti]), 5),
        "violating_windows_rpm": viol_req,
        "violating_windows_tpm": viol_tok,
        "pass": viol_req == 0 and viol_tok == 0,
        "provider_counters": data.get("counters"),
        "series": {"t0": t0, "per_second_requests": sec_cnt.tolist(), "per_second_tokens": sec_tok.tolist()},
        "limit_history": hist,
    }


def fetch(simulator_url: str, model: str) -> dict:
    with urllib.request.urlopen(f"{simulator_url.rstrip('/')}/v1/audit/{model}", timeout=60) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser(description="Independent rolling-60s limit auditor")
    ap.add_argument("--simulator", default="http://127.0.0.1:9000")
    ap.add_argument("--model", action="append", required=True)
    a = ap.parse_args()
    ok = True
    for m in a.model:
        res = audit(fetch(a.simulator, m))
        res.pop("series", None)
        print(json.dumps(res, indent=2))
        ok &= res["pass"]
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
