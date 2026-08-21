"""Weather at the destination, for the week or for a day.

Open-Meteo, which needs no API key, keyed off the airport's own coordinates and
timezone — both of which `airportsdata` already carries. So a destination the
agent can price flights to is automatically a destination it can give weather for,
with no second lookup table to keep in step with the first.

Everything here is real data. The condition words are a fixed mapping from WMO
weather codes, not a model's description of a number, because "light rain" and
"heavy rain" are the difference between taking a jacket and changing the day — and
a model paraphrasing a forecast is a way to be wrong about something checkable.

Times are the destination's local days. A forecast for "Tuesday" has to mean
Tuesday where they are going, which is why the airport's timezone is passed to the
API rather than the dates being interpreted here.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime

from control_plane.logging_setup import log_event

from .airports import _dataset
from .places import spoken_name

API = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 8
TIMEOUT_S = 8.0

# WMO weather codes -> (words to say, icon name for the card).
#
# A fixed table rather than a model's interpretation: these drive both what is
# said and what is drawn, and a forecast is exactly the kind of checkable fact
# that should not pass through a paraphrase.
WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "sun.max.fill"),
    1: ("Mostly clear", "sun.max.fill"),
    2: ("Partly cloudy", "cloud.sun.fill"),
    3: ("Cloudy", "cloud.fill"),
    45: ("Foggy", "cloud.fog.fill"),
    48: ("Freezing fog", "cloud.fog.fill"),
    51: ("Light drizzle", "cloud.drizzle.fill"),
    53: ("Drizzle", "cloud.drizzle.fill"),
    55: ("Heavy drizzle", "cloud.drizzle.fill"),
    56: ("Freezing drizzle", "cloud.sleet.fill"),
    57: ("Freezing drizzle", "cloud.sleet.fill"),
    61: ("Light rain", "cloud.rain.fill"),
    63: ("Rain", "cloud.rain.fill"),
    65: ("Heavy rain", "cloud.heavyrain.fill"),
    66: ("Freezing rain", "cloud.sleet.fill"),
    67: ("Freezing rain", "cloud.sleet.fill"),
    71: ("Light snow", "cloud.snow.fill"),
    73: ("Snow", "cloud.snow.fill"),
    75: ("Heavy snow", "cloud.snow.fill"),
    77: ("Snow grains", "cloud.snow.fill"),
    80: ("Light showers", "cloud.sun.rain.fill"),
    81: ("Showers", "cloud.rain.fill"),
    82: ("Heavy showers", "cloud.heavyrain.fill"),
    85: ("Snow showers", "cloud.snow.fill"),
    86: ("Heavy snow showers", "cloud.snow.fill"),
    95: ("Thunderstorms", "cloud.bolt.rain.fill"),
    96: ("Thunderstorms with hail", "cloud.bolt.rain.fill"),
    99: ("Thunderstorms with hail", "cloud.bolt.rain.fill"),
}


def describe_code(code: int | None) -> tuple[str, str]:
    if code is None:
        return ("Unknown", "cloud.fill")
    return WMO.get(int(code), ("Unsettled", "cloud.fill"))


@dataclass(frozen=True)
class Day:
    date: str            # ISO, local to the destination
    weekday: str         # "Tue"
    high_c: int
    low_c: int
    condition: str
    icon: str

    @property
    def spoken(self) -> str:
        return f"{self.condition.lower()}, {self.low_c} to {self.high_c} degrees"


@dataclass(frozen=True)
class Forecast:
    code: str = ""
    city: str = ""
    condition: str = ""
    now_c: int | None = None
    days: tuple[Day, ...] = field(default_factory=tuple)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.days)

    def day_for(self, when: str | None) -> Day | None:
        """A specific day, by ISO date or by weekday name.

        Accepts both because people say both — "the fourth" and "Saturday" are the
        same question, and the agent should not have to convert one into the other.
        """
        if not when or not self.days:
            return None
        wanted = when.strip().lower()
        for day in self.days:
            if day.date == wanted or day.weekday.lower() == wanted[:3]:
                return day
        return None

    def say(self, when: str | None = None) -> str:
        """One or two sentences, for speech."""
        if not self.ok:
            return f"I couldn't get the weather for {self.city or 'there'}."
        if when:
            day = self.day_for(when)
            if day is None:
                return (
                    f"I only have the next {len(self.days)} days for {self.city}. "
                    f"{self.days[0].weekday} is {self.days[0].spoken}."
                )
            return f"{day.weekday} in {self.city} is {day.spoken}."

        highs = [d.high_c for d in self.days]
        lows = [d.low_c for d in self.days]
        wet = [d for d in self.days if "rain" in d.condition.lower()
               or "shower" in d.condition.lower() or "storm" in d.condition.lower()]
        line = (
            f"{self.city} is {min(lows)} to {max(highs)} degrees over the week, "
            f"mostly {self._dominant().lower()}."
        )
        if wet:
            days = ", ".join(d.weekday for d in wet[:3])
            line += f" Rain around {days}."
        return line

    def _dominant(self) -> str:
        counts: dict[str, int] = {}
        for day in self.days:
            counts[day.condition] = counts.get(day.condition, 0) + 1
        return max(counts, key=lambda k: counts[k]) if counts else "unsettled"

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "city": self.city,
            "condition": self.condition,
            "now_c": self.now_c,
            "days": [
                {
                    "date": d.date, "weekday": d.weekday,
                    "high_c": d.high_c, "low_c": d.low_c,
                    "condition": d.condition, "icon": d.icon,
                }
                for d in self.days
            ],
        }


async def forecast_for(code: str, *, call_id: str = "weather") -> Forecast:
    """Never raises. An empty forecast means the agent says it could not get it.

    Celsius, because the destinations are Asia and the callers are in India. The
    design's Fahrenheit is a US widget screenshot, not a requirement.
    """
    airport = _dataset().get((code or "").upper())
    city = spoken_name(code) or code
    if not airport:
        return Forecast(code=code, city=city, error="unknown_airport")

    query = urllib.parse.urlencode({
        "latitude": airport["lat"],
        "longitude": airport["lon"],
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "current": "temperature_2m,weather_code",
        # The destination's own timezone: a forecast for "Tuesday" has to mean
        # Tuesday where they are going.
        "timezone": airport["tz"] or "UTC",
        "forecast_days": FORECAST_DAYS,
    })

    def fetch() -> dict:
        with urllib.request.urlopen(f"{API}?{query}", timeout=TIMEOUT_S) as response:
            return json.loads(response.read() or b"{}")

    try:
        # Blocking urllib in a thread: no new dependency for one GET, and the
        # event loop is not held while it waits.
        payload = await asyncio.wait_for(
            asyncio.to_thread(fetch), timeout=TIMEOUT_S + 2
        )
    except (asyncio.TimeoutError, urllib.error.URLError, OSError,
            json.JSONDecodeError) as exc:
        log_event("weather.failed", level="warn", call_id=call_id,
                  code=code, error=str(exc)[:120])
        return Forecast(code=code, city=city, error="unreachable")

    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []

    days: list[Day] = []
    for index, iso in enumerate(times):
        if index >= len(highs) or index >= len(lows):
            break
        condition, icon = describe_code(
            codes[index] if index < len(codes) else None
        )
        days.append(Day(
            date=iso,
            weekday=_weekday(iso),
            high_c=round(highs[index]),
            low_c=round(lows[index]),
            condition=condition,
            icon=icon,
        ))

    current = payload.get("current") or {}
    now_condition, _ = describe_code(current.get("weather_code"))
    forecast = Forecast(
        code=(code or "").upper(),
        city=city,
        condition=now_condition,
        now_c=round(current["temperature_2m"]) if "temperature_2m" in current else None,
        days=tuple(days),
    )
    log_event("weather.ready", call_id=call_id, code=forecast.code,
              city=city, days=len(days), condition=now_condition)
    return forecast


def _weekday(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%a")
    except ValueError:
        try:
            return datetime.fromisoformat(iso).strftime("%a")
        except ValueError:
            return "—"
