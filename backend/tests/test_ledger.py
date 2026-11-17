import random

from inferq.ledger import SlotLedger


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_never_exceeds_rolling_window_under_overload():
    clk = FakeClock()
    led = SlotLedger(rpm=6000, tpm=10**9, guard_fraction=0.0, clock=clk)
    sends = []
    for _ in range(3000):            # 300 s in 100 ms steps, always demand
        while led.can_send(1, 10):
            led.record(1, 10)
            sends.append(clk.t)
        clk.t += 0.1
    # brute-force every 60 s window (aligned to send times)
    sends.sort()
    j = 0
    for i, t in enumerate(sends):
        while sends[j] <= t - 60.0:
            j += 1
        assert i - j + 1 <= 6000
    # and the ledger actually used the capacity (>= 95%)
    assert len(sends) >= 0.95 * 6000 * 5


def test_pacing_spreads_sends():
    clk = FakeClock()
    led = SlotLedger(rpm=60000, tpm=10**9, guard_fraction=0.0, clock=clk)
    n = 0
    while led.can_send(1, 1):
        led.record(1, 1)
        n += 1
    assert n == 100  # 60000 / 600 buckets: no burst of the whole minute at once


def test_limit_drop_carries_trailing_usage():
    clk = FakeClock()
    led = SlotLedger(rpm=30000, tpm=10**9, guard_fraction=0.0, clock=clk)
    for _ in range(300):  # 30 s at full pace -> 15000 in window
        while led.can_send(1, 1):
            led.record(1, 1)
        clk.t += 0.1
    led.set_limits(5000, 10**9)
    assert not led.can_send(1, 1)                 # debt: 15000 > 5000
    assert led.seconds_until_fits(1, 1) > 30.0      # must wait for the window to drain


def test_refund_goes_to_original_bucket_and_is_dropped_after_expiry():
    clk = FakeClock()
    led = SlotLedger(rpm=100, tpm=1000, guard_fraction=0.0, clock=clk, pace=False)
    b = led.record(1, 1000)
    clk.t += 1.0
    led.reconcile(b, 1000, 600)                    # refund 400 into the original bucket
    assert led.usage() == (1, 600)
    clk.t += 61.0                                   # original bucket expired
    b2 = led.record(1, 500)
    clk.t += 61.0
    led.reconcile(b2, 500, 100)                     # too late: refund dropped, no negative usage
    assert led.usage() == (0, 0)


def test_underestimate_charged_to_current_bucket():
    clk = FakeClock()
    led = SlotLedger(rpm=100, tpm=10_000, guard_fraction=0.0, clock=clk, pace=False)
    b = led.record(1, 1000)
    clk.t += 0.5
    led.reconcile(b, 1000, 1200)
    assert led.usage() == (1, 1200)


def test_zero_limit_rejects_everything():
    led = SlotLedger(rpm=0, tpm=0, guard_fraction=0.0, clock=FakeClock())
    assert not led.fits_ever(1)
    assert not led.can_send(1, 1)


def test_randomised_tokens_respect_tpm():
    clk = FakeClock()
    led = SlotLedger(rpm=10**6, tpm=600_000, guard_fraction=0.0, clock=clk)
    rnd = random.Random(3)
    log = []
    for _ in range(1500):
        while True:
            tok = rnd.randint(1, 3000)
            if not led.can_send(1, tok):
                break
            led.record(1, tok)
            log.append((clk.t, tok))
        clk.t += 0.1
    for t, _ in log:
        assert sum(k for s, k in log if t - 60.0 < s <= t) <= 600_000
