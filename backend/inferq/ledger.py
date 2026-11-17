"""Strict rolling-60s slot ledger.

Each model owns two rings of 100 ms buckets (requests and tokens).  A send is
allowed only if the sum over the trailing 601 buckets (60.1 s, i.e. every real
60-second interval that could contain this send, regardless of sub-bucket
offset) plus the new cost stays within the *effective* limit
(limit * (1 - guard_fraction)).

Why not a token bucket?  A bucket with a burst allowance can legally emit
burst + 60 s of refill inside one 60 s window, which breaks the brief's
"no observed 60-second period exceeds either limit".  A fixed window can emit
2x the limit across a boundary.  The ring is exact at 100 ms resolution.

Reconciliation rule (estimated -> actual tokens):
  * actual < estimate: refund to the ORIGINAL bucket if it is still inside the
    window, otherwise drop the refund.  Refunding into the *current* bucket
    would understate trailing usage once the original bucket ages out.
  * actual > estimate: charge the excess to the CURRENT bucket.

Limit drops: trailing usage keeps counting, so a lowered limit simply blocks
sends until enough of the window ages out ("debt carry").
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

BUCKET_S = 0.1
WINDOW_BUCKETS = 600            # 60 s
RING = WINDOW_BUCKETS + 1       # 601: covers any 60 s interval at sub-bucket offset


@dataclass
class LimitEpoch:
    at: float
    rpm: int
    tpm: int


class SlotLedger:
    def __init__(self, rpm: int, tpm: int, guard_fraction: float = 0.01, clock=time.time,
                 pace: bool = True):
        self._clock = clock
        self.guard_fraction = guard_fraction
        self.pace = pace
        self._req = [0] * RING
        self._tok = [0] * RING
        self._req_sum = 0
        self._tok_sum = 0
        self._head = int(self._clock() / BUCKET_S)
        self.epochs: list[LimitEpoch] = []
        self.set_limits(rpm, tpm)

    # ------------------------------------------------------------------ limits
    def set_limits(self, rpm: int, tpm: int) -> None:
        self.rpm = int(rpm)
        self.tpm = int(tpm)
        self.eff_rpm = int(math.floor(self.rpm * (1.0 - self.guard_fraction)))
        self.eff_tpm = int(math.floor(self.tpm * (1.0 - self.guard_fraction)))
        self.epochs.append(LimitEpoch(self._clock(), self.rpm, self.tpm))

    @staticmethod
    def _bucket_cap(b: int, eff: int) -> int:
        """Exact fractional pacing: bucket b may hold floor((b+1)*eff/600) - floor(b*eff/600)
        sends, so any 600 consecutive buckets allow exactly eff (no rounding drift)."""
        return (b + 1) * eff // WINDOW_BUCKETS - b * eff // WINDOW_BUCKETS

    # ------------------------------------------------------------------ window
    def _advance(self) -> int:
        b = int(self._clock() / BUCKET_S)
        if b - self._head >= RING:
            # whole window expired: reset
            self._req = [0] * RING
            self._tok = [0] * RING
            self._req_sum = self._tok_sum = 0
            self._head = b
            return b
        while self._head < b:
            self._head += 1
            i = self._head % RING
            self._req_sum -= self._req[i]
            self._tok_sum -= self._tok[i]
            self._req[i] = 0
            self._tok[i] = 0
        return b

    def usage(self) -> tuple[int, int]:
        self._advance()
        return self._req_sum, self._tok_sum

    def fits_ever(self, tokens: int) -> bool:
        """Could a request of this size EVER be sent under the current limit?"""
        return self.eff_rpm >= 1 and tokens <= self.eff_tpm

    def can_send(self, n: int, tokens: int) -> bool:
        b = self._advance()
        if self._req_sum + n > self.eff_rpm or self._tok_sum + tokens > self.eff_tpm:
            return False
        if self.pace:
            # Smooth pacing: at most limit/600 per 100 ms bucket, so a fresh window is
            # not burned in the first seconds (which would cause a 10 s on / off sawtooth).
            i = b % RING
            if self._req[i] + n > self._bucket_cap(b, self.eff_rpm):
                return False
            if self._tok[i] > 0 and self._tok[i] + tokens > self._bucket_cap(b, self.eff_tpm):
                return False
        return True

    def headroom(self) -> tuple[int, int]:
        self._advance()
        return self.eff_rpm - self._req_sum, self.eff_tpm - self._tok_sum

    def record(self, n: int, tokens: int) -> int:
        """Charge a send to the current bucket.  Returns the bucket id (for reconciliation)."""
        b = self._advance()
        i = b % RING
        self._req[i] += n
        self._tok[i] += tokens
        self._req_sum += n
        self._tok_sum += tokens
        return b

    def reconcile(self, bucket: int, estimated: int, actual: int) -> None:
        delta = int(actual) - int(estimated)
        if delta == 0:
            return
        b = self._advance()
        if delta < 0:
            if bucket > b - RING:  # original bucket still inside the window
                i = bucket % RING
                refund = min(-delta, self._tok[i])
                self._tok[i] -= refund
                self._tok_sum -= refund
            # else: original bucket already expired -> drop the refund
        else:
            i = b % RING
            self._tok[i] += delta
            self._tok_sum += delta

    def seconds_until_fits(self, n: int, tokens: int) -> float:
        """Earliest time (seconds from now) at which a send of (n, tokens) fits,
        assuming no further sends.  inf if it can never fit."""
        if not self.fits_ever(tokens) or n > self.eff_rpm:
            return math.inf
        b = self._advance()
        need_req = self._req_sum + n - self.eff_rpm
        need_tok = self._tok_sum + tokens - self.eff_tpm
        if need_req <= 0 and need_tok <= 0:
            return 0.0
        freed_req = freed_tok = 0
        # oldest bucket in window is b - RING + 1; it leaves at the next advance
        for k in range(1, RING + 1):
            i = (b - RING + k) % RING
            freed_req += self._req[i]
            freed_tok += self._tok[i]
            if freed_req >= need_req and freed_tok >= need_tok:
                return k * BUCKET_S
        return RING * BUCKET_S
