"""In-process sliding-window rate limiting.

Deliberately simple and deliberately in-memory: correct for a single control
plane instance, which is all v1 runs. Move to Redis before running more than
one replica, or the limits silently multiply by the replica count.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import HTTPException, status

from .logging_setup import Events, log_event

_WINDOW_SECONDS = 60.0
_hits: dict[str, Deque[float]] = defaultdict(deque)


def check(bucket: str, key: str, limit_per_min: int, *, call_id: str | None = None) -> None:
    """Raise 429 if `key` has exceeded `limit_per_min` in the last minute."""
    now = time.monotonic()
    q = _hits[f"{bucket}:{key}"]

    while q and now - q[0] > _WINDOW_SECONDS:
        q.popleft()

    if len(q) >= limit_per_min:
        retry_after = int(_WINDOW_SECONDS - (now - q[0])) + 1
        log_event(
            Events.ERROR_RATE_LIMITED,
            level="warn",
            call_id=call_id,
            bucket=bucket,
            key=key,
            limit=limit_per_min,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limit exceeded for {bucket}",
            headers={"Retry-After": str(retry_after)},
        )

    q.append(now)


def reset() -> None:
    """Test helper."""
    _hits.clear()
