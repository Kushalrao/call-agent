"""Departure and arrival times as a person at the airport would read them.

The platforms disagree about what a timestamp means. Cleartrip publishes local
time with an offset (`2026-12-07T08:10+05:30`); Ixigo publishes UTC (`...Z`). Read
naively — taking the wall clock out of the string, which is what the iOS card does
— an Ixigo departure from Bangalore shows as 02:40 instead of 08:10.

That was tolerable as a display bug on a card someone could sanity-check. It is
not tolerable spoken aloud: a time you mishear cannot be re-read, and "your
flight leaves at twenty to three in the morning" is a genuinely costly thing to be
wrong about. So times were excluded from the voice path entirely until this
existed.

The fix is possible because the airport dataset carries an IANA timezone per
airport. A UTC instant plus the airport's zone gives the local wall clock, which
is the only reading anyone cares about — nobody asks what time their flight
leaves in UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse(value: str) -> datetime | None:
    """Parse the shapes these platforms actually emit."""
    text = (value or "").strip()
    if not text:
        return None
    # fromisoformat handles offsets and fractional seconds; it wants +00:00 for Z.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _zone(iata: str | None) -> ZoneInfo | None:
    if not iata:
        return None
    try:
        from agent.airports import _dataset  # local import: keeps the bridge light
    except Exception:  # noqa: BLE001
        return None
    airport = _dataset().get(iata.upper())
    if not airport or not airport.get("tz"):
        return None
    try:
        return ZoneInfo(airport["tz"])
    except (ZoneInfoNotFoundError, ValueError):
        return None


def to_local(value: str, iata: str | None) -> str:
    """An ISO timestamp rewritten as local time at `iata`, offset included.

    A value that already carries an offset is left alone: the platform that wrote
    it meant the airport's local time, and re-deriving it from a timezone database
    would only introduce a way to be wrong. Only UTC values are converted.

    Anything unparseable comes back untouched — a wrong-looking string is easier
    to diagnose than a silently invented one.
    """
    parsed = _parse(value)
    if parsed is None:
        return value

    # No timezone at all: assume it is already local, which is what every
    # platform means by a naive timestamp.
    if parsed.tzinfo is None:
        return value

    # Carries a real offset already, and it is not UTC — trust it.
    offset = parsed.utcoffset()
    if offset not in (None, timedelta(0)):
        return value

    zone = _zone(iata)
    if zone is None:
        return value
    return parsed.astimezone(zone).isoformat()


def spoken_time(value: str, iata: str | None = None) -> str | None:
    """"8:10 am" — what the agent should say. None when there is no usable time.

    Deliberately not seconds, and deliberately not 24-hour: this is read out loud.
    """
    parsed = _parse(to_local(value, iata))
    if parsed is None:
        return None
    hour = parsed.hour % 12 or 12
    suffix = "am" if parsed.hour < 12 else "pm"
    return f"{hour}:{parsed.minute:02d} {suffix}"
