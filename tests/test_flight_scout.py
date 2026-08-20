"""Cleartrip URL building and response parsing.

The fixture is a trimmed slice of a **real** `/flight/search/v2` capture
(BLR->DEL, 2026-09-15, taken 2026-08-21), so these tests fail if Cleartrip
changes the response shape — which is the failure worth being warned about.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from flight_scout.cleartrip import is_international, parse, search_url

FIXTURE = Path(__file__).parent / "fixtures" / "cleartrip_search_v2.json"


@pytest.fixture(scope="module")
def body() -> dict:
    return json.loads(FIXTURE.read_text())


# --- URL building -----------------------------------------------------------


def test_url_matches_lib_route_format():
    """Must match buildSearchUrl('cleartrip', route) from lib/route.js — this is
    the URL Cleartrip's own results page reads its parameters from."""
    url = search_url("blr", "del", "2026-09-15")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://www.cleartrip.com/flights/results?")
    assert q["from"] == ["BLR"] and q["to"] == ["DEL"]
    assert q["depart_date"] == ["15/09/2026"]  # DD/MM/YYYY, not ISO
    assert q["class"] == ["Economy"]
    assert q["adults"] == ["1"]
    assert "return_date" not in q


def test_round_trip_adds_return_date():
    q = urllib.parse.parse_qs(
        urllib.parse.urlparse(
            search_url("BLR", "DEL", "2026-09-15", return_date="2026-09-22")
        ).query
    )
    assert q["return_date"] == ["22/09/2026"]


def test_intl_flag_is_derived_not_guessed():
    """Cleartrip 400s with SEARCH_VALIDATION_FAILED when this flag disagrees
    with the route, so it cannot be defaulted."""
    assert is_international("BLR", "DEL") is False
    assert is_international("BLR", "DPS") is True
    assert is_international("LHR", "JFK") is True

    domestic = urllib.parse.parse_qs(urllib.parse.urlparse(search_url("BLR", "DEL", "2026-09-15")).query)
    overseas = urllib.parse.parse_qs(urllib.parse.urlparse(search_url("BLR", "DPS", "2026-09-15")).query)
    assert domestic["intl"] == ["n"]
    assert overseas["intl"] == ["y"]


def test_cabin_labels():
    for cabin, label in [
        ("BUSINESS", "Business"),
        ("PREMIUM_ECONOMY", "Premium Economy"),
        ("FIRST", "First"),
        ("nonsense", "Economy"),  # unknown falls back rather than erroring
    ]:
        q = urllib.parse.parse_qs(
            urllib.parse.urlparse(search_url("BLR", "DEL", "2026-09-15", cabin=cabin)).query
        )
        assert q["class"] == [label]


# --- parsing real payload ---------------------------------------------------


def test_parses_real_capture(body):
    res = parse(body, expect_destination="DEL")
    assert res["ok"] is True
    assert res["flights"], "no flights parsed from a real capture"
    for f in res["flights"]:
        assert f["minPrice"] > 0
        assert f["departIso"] and f["arriveIso"]
        assert f["flightNumbers"]
        assert f["destination"] == "DEL"


def test_price_comes_from_the_fare_chain(body):
    """cards -> subTravelOptions -> fares -> pricing.totalPricing.totalPrice.
    `travelOptionIdsToFareIdsMap` is empty in real responses, so relying on it
    would silently yield zero priced flights."""
    assert body["travelOptionIdsToFareIdsMap"] == {}
    res = parse(body, expect_destination="DEL")
    prices = [f["minPrice"] for f in res["flights"]]
    assert all(p > 500 for p in prices), f"implausible prices: {prices}"


def test_nearby_airport_results_are_filtered(body):
    """A BLR->DEL search returns BLR->HDO cards too. Showing them would offer a
    suspiciously cheap flight to a different city."""
    unfiltered = parse(body)
    filtered = parse(body, expect_destination="DEL")
    assert len(filtered["flights"]) < len(unfiltered["flights"])
    assert filtered["diagnostics"]["skipped_nearby_airport"] >= 1
    assert {f["destination"] for f in filtered["flights"]} == {"DEL"}


def test_airline_names_resolved_from_metadata(body):
    res = parse(body, expect_destination="DEL")
    carriers = {c for f in res["flights"] for c in f["airlines"]}
    # Names, not raw two-letter codes.
    assert any(len(c) > 2 for c in carriers), f"got only codes: {carriers}"


def test_cheapest_first(body):
    res = parse(body, expect_destination="DEL")
    prices = [f["minPrice"] for f in res["flights"]]
    assert prices == sorted(prices)


def test_duration_and_stops(body):
    res = parse(body, expect_destination="DEL")
    for f in res["flights"]:
        assert isinstance(f["stops"], int) and f["stops"] >= 0
        if f["durationMin"] is not None:
            assert 30 < f["durationMin"] < 3000


def test_bad_input_is_typed_not_thrown():
    assert parse("not json")["ok"] is False
    assert "json parse failed" in parse("not json")["error"]

    empty = parse({"cards": {"J1": []}})
    assert empty["ok"] is False
    assert empty["error"] == "no cards.J1"

    # Missing fares entirely: flights exist but none are priced. Must report
    # rather than emit priceless options.
    no_fares = parse(
        {
            "cards": {"J1": [{"travelOptionId": "x", "summary": {"flights": [], "stops": 0}}]},
            "subTravelOptions": {},
            "fares": {},
        }
    )
    assert no_fares["ok"] is False
    assert no_fares["diagnostics"]["skipped_unpriced"] == 1


def test_feeds_the_bridge_normalizer(body):
    """One normalizer serves both the browser path and the extension path, so
    the shapes must line up."""
    from flight_bridge.normalize import to_widget_payload

    res = parse(body, expect_destination="DEL")
    record = {
        "id": "scout-1",
        "route": {"from": "BLR", "to": "DEL", "date": "2026-09-15", "returnDate": None},
        "sites": {"cleartrip": {"status": "complete", "flights": res["flights"]}},
    }
    payload = to_widget_payload(record)
    assert payload["route"] == {"origin": "BLR", "destination": "DEL"}
    ct = payload["platforms"]["Cleartrip"]
    assert 1 <= len(ct["options"]) <= 3
    first = ct["options"][0]
    assert first["price"]["currency"] == "INR"
    assert first["carrier"] and first["flight"]
    # Platforms the extension never ran still appear, so the agent can say what
    # it did and did not check.
    assert payload["platforms"]["Ixigo"]["options"] == []
