"""Spend ceilings (agent/budget.py).

The thing being defended against is not steady-state cost — that is a number you
can estimate. It is a loop: a retry that never gives up, a classifier that
re-fires on its own output. So these tests are mostly about refusal.
"""

from __future__ import annotations

import json

import pytest

from agent.budget import (
    Budget,
    BudgetExceeded,
    DailyLedger,
    PRICING,
    Usage,
    price,
)


# --- pricing ---------------------------------------------------------------


def test_pricing_matches_the_published_rates():
    assert PRICING["claude-haiku-4-5"] == (1.00, 5.00)
    assert PRICING["claude-sonnet-5"] == (3.00, 15.00)


def test_a_typo_in_a_model_name_cannot_under_report_spend():
    """Unknown models are priced at the most expensive rate we know, so a bad
    model string shows up as a scary number rather than as free."""
    unknown = price("claude-does-not-exist", input_tokens=1_000_000)
    most_expensive = max(rate_in for rate_in, _ in PRICING.values())
    assert unknown == pytest.approx(most_expensive)


def test_cache_read_is_cheaper_than_fresh_input():
    fresh = price("claude-haiku-4-5", input_tokens=1000)
    cached = price("claude-haiku-4-5", cache_read_tokens=1000)
    assert cached == pytest.approx(fresh * 0.10)


def test_measured_classifier_cost():
    """Guards the real number from EXECUTION_PLAN: 579 in / 19 out on Haiku."""
    per_utterance = price("claude-haiku-4-5", input_tokens=579, output_tokens=19)
    assert per_utterance == pytest.approx(0.000674, abs=1e-6)
    # A 120-utterance ten-minute call.
    assert per_utterance * 120 == pytest.approx(0.081, abs=0.001)


# --- ceilings are refusals, not warnings -----------------------------------


def budget(tmp_path, **kw) -> Budget:
    defaults = dict(call_id="c1", ledger=DailyLedger(tmp_path / "spend.json"))
    defaults.update(kw)
    return Budget(**defaults)


def test_call_count_ceiling_refuses(tmp_path):
    b = budget(tmp_path, max_calls=3)
    for _ in range(3):
        b.check()
        b.record("claude-haiku-4-5", stage="classifier", input_tokens=100, output_tokens=10)
    with pytest.raises(BudgetExceeded, match="max_calls_per_call"):
        b.check()


def test_per_call_usd_ceiling_refuses(tmp_path):
    b = budget(tmp_path, max_usd_per_call=0.001)
    b.check()
    b.record("claude-sonnet-5", stage="turn", input_tokens=1000, output_tokens=100)
    with pytest.raises(BudgetExceeded, match="max_usd_per_call"):
        b.check()


def test_the_estimate_is_counted_before_the_request(tmp_path):
    """Checking only what has already been spent would let one very large request
    through after the budget was nearly exhausted."""
    b = budget(tmp_path, max_usd_per_call=0.01)
    b.check(estimated_usd=0.005)          # fits
    with pytest.raises(BudgetExceeded):
        b.check(estimated_usd=0.02)       # does not


def test_once_stopped_it_stays_stopped(tmp_path):
    b = budget(tmp_path, max_calls=1)
    b.check()
    b.record("claude-haiku-4-5", stage="s", input_tokens=1, output_tokens=1)
    with pytest.raises(BudgetExceeded):
        b.check()
    assert b.stopped
    # Not a transient refusal that a retry loop could grind past.
    with pytest.raises(BudgetExceeded):
        b.check()


def test_disabled_refuses_everything(tmp_path):
    with pytest.raises(BudgetExceeded, match="llm_disabled"):
        budget(tmp_path, enabled=False).check()


# --- the daily ceiling must survive a restart ------------------------------


def test_daily_spend_persists_across_processes(tmp_path):
    """The daily ceiling exists to stop the exact thing that would blow through
    it — a process that keeps crashing and restarting. An in-memory counter
    resets on every restart, which is backwards."""
    path = tmp_path / "spend.json"
    first = Budget(call_id="c1", ledger=DailyLedger(path))
    first.record("claude-sonnet-5", stage="turn", input_tokens=1_000_000)

    # A brand new process, a brand new call.
    second = Budget(call_id="c2", ledger=DailyLedger(path))
    assert second.ledger.spent_today() == pytest.approx(3.0)


def test_daily_ceiling_refuses_a_fresh_call(tmp_path):
    path = tmp_path / "spend.json"
    Budget(call_id="c1", ledger=DailyLedger(path)).record(
        "claude-sonnet-5", stage="turn", input_tokens=1_000_000
    )
    fresh = Budget(call_id="c2", max_usd_per_day=1.0, ledger=DailyLedger(path))
    with pytest.raises(BudgetExceeded, match="max_usd_per_day"):
        fresh.check()


def test_a_corrupt_ledger_does_not_crash_a_call(tmp_path):
    path = tmp_path / "spend.json"
    path.write_text("{not json")
    assert DailyLedger(path).spent_today() == 0.0


def test_ledger_prunes_old_days(tmp_path):
    path = tmp_path / "spend.json"
    path.write_text(json.dumps({f"2020-01-{d:02d}": {"usd": 1.0, "calls": 1}
                                for d in range(1, 26)}))
    ledger = DailyLedger(path)
    ledger.record(0.01)
    assert len(json.loads(path.read_text())) <= 15


# --- accounting -----------------------------------------------------------


def test_cache_hit_ratio_reveals_a_cache_that_is_not_working():
    """Measured reality: the classifier prompt is under Haiku's 2048-token cache
    minimum, so this ratio is 0 and cost is the uncached cost. The number exists
    so that stays visible rather than being assumed away."""
    u = Usage()
    u.add("claude-haiku-4-5", input_tokens=579, output_tokens=19)
    assert u.as_dict()["cache_hit_ratio"] == 0.0

    u2 = Usage()
    u2.add("claude-haiku-4-5", input_tokens=100, cache_read_tokens=900, output_tokens=19)
    assert u2.as_dict()["cache_hit_ratio"] == 0.9


def test_summary_reports_why_it_stopped(tmp_path):
    b = budget(tmp_path, max_calls=0)
    with pytest.raises(BudgetExceeded):
        b.check()
    assert b.summary()["stopped"] == "max_calls_per_call"
