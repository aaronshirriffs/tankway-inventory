"""
Per-API-key in-memory rate limiting.

Three configurable knobs per key (all set from the admin panel):
  - rate_limit_per_minute : sustained steady rate
  - burst_allowance       : extra requests tolerated in a short spike
  - rate_limit_daily      : hard cap of requests per UTC day

Implemented as a token bucket (per-minute + burst) plus a daily counter.
State is in-memory and per key (not per IP); it resets on service restart,
which is acceptable for this scale.

NOTE: a custom limiter is used here rather than Flask-Limiter's decorators
because the per-key, admin-configurable burst-vs-sustained behaviour can't be
expressed cleanly through Flask-Limiter's static/dynamic limit strings. It
remains purely in-memory as specified.
"""
import threading

_lock = threading.Lock()
_buckets = {}   # token -> {tokens, capacity, refill_per_sec, last_ts, day, day_count}


def _bucket(token, rpm, burst, monotonic):
    capacity = max(1, rpm + burst)
    refill_per_sec = rpm / 60.0
    b = _buckets.get(token)
    if b is None:
        b = {
            "tokens": float(capacity),
            "capacity": capacity,
            "refill_per_sec": refill_per_sec,
            "last_ts": monotonic,
            "day": None,
            "day_count": 0,
        }
        _buckets[token] = b
    else:
        # Reconfigure live if the admin changed the limits.
        b["capacity"] = capacity
        b["refill_per_sec"] = refill_per_sec
        if b["tokens"] > capacity:
            b["tokens"] = float(capacity)
    return b


def check(token, rpm, burst, daily, monotonic, day_key, seconds_to_day_reset):
    """
    Decide whether a request is allowed.

    Args:
        token                 : the API key
        rpm, burst, daily     : the key's configured limits
        monotonic             : a monotonic clock value (seconds)
        day_key               : a string identifying the current day (e.g. "2026-06-06")
        seconds_to_day_reset  : seconds until the daily counter rolls over

    Returns (allowed: bool, retry_after_seconds: int).
    """
    with _lock:
        b = _bucket(token, rpm, burst, monotonic)

        # ----- daily cap -----
        if b["day"] != day_key:
            b["day"] = day_key
            b["day_count"] = 0
        if daily and b["day_count"] >= daily:
            return False, max(1, int(seconds_to_day_reset))

        # ----- token-bucket refill -----
        elapsed = max(0.0, monotonic - b["last_ts"])
        b["last_ts"] = monotonic
        b["tokens"] = min(b["capacity"], b["tokens"] + elapsed * b["refill_per_sec"])

        if b["tokens"] < 1.0:
            rate = b["refill_per_sec"] or (1 / 60.0)
            retry = (1.0 - b["tokens"]) / rate
            return False, max(1, int(retry + 0.999))

        # ----- consume -----
        b["tokens"] -= 1.0
        b["day_count"] += 1
        return True, 0


def reset(token):
    with _lock:
        _buckets.pop(token, None)
