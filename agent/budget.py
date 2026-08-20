"""Hard ceilings on model spend, and the accounting to see it (spec section 11).

The question this module answers: what stops a bug from spending real money?

Steady-state cost is not the risk. The risk is a loop — a classifier that
re-fires on its own output, a retry that never gives up, a call that never ends.
Steady state is a number you can estimate; a loop is unbounded. So every ceiling
here is a **refusal**, not a warning, and the daily one is persisted so a crash
loop cannot reset it by restarting.

Three layers, tightest first:

  per-call call count   a bounded conversation makes a bounded number of requests
  per-call USD         catches an unusually expensive single call
  per-day USD          the backstop; survives process restart

And one escape hatch that costs nothing: `LLM_OFFLINE=true` returns canned
results, so the replay harness and every test can exercise the decision layer
without a single billable token.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from control_plane.logging_setup import Events, log_event

# USD per million tokens. Cache write is 1.25x input, cache read is 0.1x input.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


class BudgetExceeded(RuntimeError):
    """Raised instead of making a request. Callers treat this like a timeout:
    degrade to no-trigger and let the call continue (spec section 10)."""


def price(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Cost in USD. Unknown models are priced at the most expensive rate we know,
    so a typo in a model name cannot silently under-report spend."""
    rate_in, rate_out = PRICING.get(model, max(PRICING.values()))
    return (
        input_tokens * rate_in
        + output_tokens * rate_out
        + cache_write_tokens * rate_in * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * rate_in * CACHE_READ_MULTIPLIER
    ) / 1_000_000


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    usd: float = 0.0

    def add(self, model: str, **tokens: int) -> float:
        cost = price(model, **tokens)
        self.calls += 1
        self.input_tokens += tokens.get("input_tokens", 0)
        self.output_tokens += tokens.get("output_tokens", 0)
        self.cache_write_tokens += tokens.get("cache_write_tokens", 0)
        self.cache_read_tokens += tokens.get("cache_read_tokens", 0)
        self.usd += cost
        return cost

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "usd": round(self.usd, 5),
            # The share of input served from cache. If this is near zero the
            # cache is not working and cost is roughly double what it should be.
            "cache_hit_ratio": round(
                self.cache_read_tokens
                / max(1, self.cache_read_tokens + self.input_tokens + self.cache_write_tokens),
                3,
            ),
        }


class DailyLedger:
    """Spend for the current UTC day, persisted.

    On disk because the whole point of a daily ceiling is to survive the thing
    that would blow through it — a process that keeps crashing and restarting.
    An in-memory counter resets on every restart, which is exactly backwards.
    """

    def __init__(self, path: str | Path = "logs/llm_spend.json") -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def spent_today(self) -> float:
        return float(self._read().get(self._today(), {}).get("usd", 0.0))

    def record(self, usd: float, *, calls: int = 1) -> float:
        with self._lock:
            data = self._read()
            day = data.setdefault(self._today(), {"usd": 0.0, "calls": 0})
            day["usd"] = round(float(day.get("usd", 0.0)) + usd, 6)
            day["calls"] = int(day.get("calls", 0)) + calls
            # Keep a fortnight; enough to spot a trend, small enough to stay fast.
            for stale in sorted(data)[:-14]:
                data.pop(stale, None)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(data, indent=1, sort_keys=True))
            except OSError as exc:
                # Never let accounting break a call — but say so loudly, because
                # an unwritable ledger means the daily ceiling is not enforced.
                log_event(Events.ERROR_INTERNAL, level="error",
                          error=f"llm ledger unwritable: {exc}")
            return day["usd"]


@dataclass
class Budget:
    """Per-call spend authority. One per call."""

    call_id: str
    max_calls: int = 150
    max_usd_per_call: float = 0.50
    max_usd_per_day: float = 10.00
    enabled: bool = True
    offline: bool = False
    ledger: DailyLedger = field(default_factory=DailyLedger)
    usage: Usage = field(default_factory=Usage)
    _stopped_reason: str | None = None

    @classmethod
    def from_env(cls, call_id: str) -> Budget:
        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name) or default)
            except ValueError:
                return default

        from control_plane.config import get_settings
        s = get_settings()
        return cls(
            call_id=call_id,
            max_calls=int(_f("LLM_MAX_CALLS_PER_CALL", 150)),
            max_usd_per_call=_f("LLM_MAX_USD_PER_CALL", 0.50),
            max_usd_per_day=_f("LLM_MAX_USD_PER_DAY", 10.00),
            enabled=(os.environ.get("LLM_ENABLED", "true").lower() != "false")
            and bool(getattr(s, "anthropic_api_key", "")),
            offline=os.environ.get("LLM_OFFLINE", "false").lower() == "true",
        )

    @property
    def stopped(self) -> bool:
        return self._stopped_reason is not None

    def check(self, *, estimated_usd: float = 0.0) -> None:
        """Called immediately before every request. Raises rather than returning
        a flag, so a caller cannot forget to look."""
        if not self.enabled:
            raise BudgetExceeded("llm_disabled")
        if self._stopped_reason:
            raise BudgetExceeded(self._stopped_reason)

        if self.usage.calls >= self.max_calls:
            self._stop("max_calls_per_call")
        elif self.usage.usd + estimated_usd > self.max_usd_per_call:
            self._stop("max_usd_per_call")
        else:
            spent = self.ledger.spent_today()
            if spent + estimated_usd > self.max_usd_per_day:
                self._stop("max_usd_per_day", detail=f"spent_today={spent:.4f}")

        if self._stopped_reason:
            raise BudgetExceeded(self._stopped_reason)

    def _stop(self, reason: str, *, detail: str | None = None) -> None:
        self._stopped_reason = reason
        log_event(
            "llm.budget_exceeded",
            level="error",
            call_id=self.call_id,
            reason=reason,
            detail=detail,
            **self.usage.as_dict(),
        )

    def record(self, model: str, *, stage: str, latency_ms: float | None = None,
               **tokens: int) -> float:
        """Called after every request, success or failure."""
        cost = self.usage.add(model, **tokens)
        day_total = self.ledger.record(cost)
        log_event(
            "llm.usage",
            call_id=self.call_id,
            stage=stage,
            model=model,
            latency_ms=latency_ms,
            usd=round(cost, 6),
            call_usd=round(self.usage.usd, 5),
            day_usd=round(day_total, 4),
            **{k: v for k, v in tokens.items() if v},
        )
        return cost

    def summary(self) -> dict:
        return {**self.usage.as_dict(), "stopped": self._stopped_reason}
