from tools.auditor import audit


def make(counts_per_bucket, history, bucket_s=0.01, start=100_000):
    buckets = list(range(start, start + len(counts_per_bucket)))
    return {"model": "m", "bucket_s": bucket_s, "buckets": buckets, "counts": counts_per_bucket,
            "tokens": [c * 1000 for c in counts_per_bucket], "limit_history": history}


def test_detects_violation():
    # 7000 requests within 10 s, limit 6000 RPM
    d = make([7] * 1000, [{"at": 0, "rpm": 6000, "tpm": 10**12}])
    r = audit(d)
    assert not r["pass"] and r["violating_windows_rpm"] > 0


def test_passes_when_within_limit():
    d = make([1] * 12000, [{"at": 0, "rpm": 6000, "tpm": 10**12}])   # 100/s for 120 s = 6000/min
    r = audit(d)
    assert r["pass"] and r["peak_window_requests"] == 6000


def test_window_spanning_limit_drop_uses_highest_limit():
    # 100/s for 60 s under 6000, then limit drops to 600 and traffic stops
    start = 100_000
    t_drop = (start + 6000) * 0.01
    d = make([1] * 6000 + [0] * 6000, [{"at": 0, "rpm": 6000, "tpm": 10**12},
                                        {"at": t_drop, "rpm": 600, "tpm": 10**12}], start=start)
    assert audit(d)["pass"]
    # but sending 6000 more right after the drop is a violation
    d2 = make([1] * 6000 + [1] * 6000, d["limit_history"], start=start)
    assert not audit(d2)["pass"]
