"""HTTP surface for the flight search, so an ElevenLabs agent can call it.

The agent runs on ElevenLabs' infrastructure and the search runs here, driving a
real Chrome with the user's own extension. So the search has to be reachable from
the public internet — which means this is a tiny, deliberately boring service with
exactly one capability and a shared secret in front of it.

Two things it is careful about.

**The response is written to be spoken.** The extension returns hundreds of
flights; a voice agent needs one sentence and a couple of alternatives. Handing a
model a large JSON blob and hoping it summarises well is how you get a agent that
reads out fare codes, so the shaping happens here where it can be tested.

**It never hangs.** A voice conversation cannot wait indefinitely, so the search
has a hard deadline and a failed search returns a speakable sentence rather than
an error the model has to interpret.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.advice import RouteAdvice, advise
from agent.airports import describe
from agent.citynote import NOTES
from agent.weather import forecast_for
from agent.places import resolve, spoken_name
from agent.resolver import resolve_with_model
from agent.search import (
    DEFAULT_ORIGIN,
    SearchCache,
    default_depart_date,
    run_search,
    to_sentence,
)
from control_plane.logging_setup import log_event

# ElevenLabs sends this as a header on every tool call. Without it the tunnel is
# an open endpoint that drives a real browser on someone's laptop.
TOOL_SECRET = os.environ.get("FLIGHT_TOOL_SECRET", "")

app = FastAPI(title="hands-free flight tool", version="1.0.0")

# Shared across calls: the aggregators throttle repeated identical queries, so a
# route fetched once in the last few minutes is answered from here.
_cache = SearchCache()
_cached_at: dict[str, float] = {}
CACHE_TTL_S = 600.0

# Advice in flight or finished, keyed by route. Started by a search, read by
# route_advice — which is what keeps a 13s opinion off the first answer.
_advice: dict[str, "asyncio.Task[RouteAdvice]"] = {}

# The most recent search, for the phone to display.
#
# Deliberately a single slot rather than per-session. The agent runs on
# ElevenLabs and the phone connects to it directly, so there is no session id
# shared between the two — and this is a one-user dev build. When it is not, this
# has to become per-conversation, and the honest way to do that is for the agent
# to pass its conversation id into the tool call.
_latest: dict[str, Any] = {}

# Routes that just failed, so a repeat asks the browser again only after a pause.
# Failures are deliberately *not* put in the result cache: caching one bad moment
# would lock a route out for the full ten minutes.
_failures: dict[str, float] = {}

# Latest weather, for the phone. Same single-slot caveat as _latest.
_weather: dict[str, Any] = {}
# Forecasts by airport, briefly. A week's outlook does not change between two
# questions in one conversation, and re-fetching would just add latency.
_weather_cache: dict[str, tuple[float, Any]] = {}
WEATHER_TTL_S = 900.0
# How many rows the agent itself is given. The card gets all of them.
MODEL_LIMIT = 12
FAILURE_COOLDOWN_S = 90.0


def _failed_recently(key: str) -> bool:
    at = _failures.get(key)
    return at is not None and (time.monotonic() - at) < FAILURE_COOLDOWN_S


class SearchRequest(BaseModel):
    destination: str = Field(description="City as spoken, e.g. 'Bali' or 'Dubai'")
    origin: str | None = Field(default=None, description="City as spoken; defaults to Bangalore")
    depart_date: str | None = Field(default=None, description="ISO yyyy-mm-dd; defaults to today + 10 days")


class Option(BaseModel):
    carrier: str
    price_inr: int
    stops: int
    platform: str
    duration_min: int | None = None
    # Local time at the airport, already worded for speech. Ixigo's UTC is
    # converted against the airport's own timezone in flight_bridge/localtime, so
    # these are the times a person standing there would read.
    departs: str | None = None
    arrives: str | None = None
    # 24-hour, for the card. "18:00" fits the designed time group; "6:00 pm" wraps.
    departs_clock: str | None = None
    arrives_clock: str | None = None
    airline_code: str | None = None


class Advice(BaseModel):
    """Judgement, not fact. Every number about a flight comes from the search;
    this is opinion, and the agent's prompt says so."""

    best_airline: str = ""
    stops_advice: str = ""
    recommendation: str = ""
    notes: list[str] = []
    watch_out: str | None = None


class Summary(BaseModel):
    """Answers to the questions people actually ask next.

    Precomputed rather than left to the model. Asked "which is cheapest" from a
    list of twelve rows a model will usually get it right and occasionally not,
    and there is no reason to leave arithmetic to inference.
    """

    total_found: int
    price_low_inr: int | None = None
    price_high_inr: int | None = None
    airlines: list[str] = []
    direct_available: bool = False
    direct_count: int = 0
    cheapest_direct: Option | None = None
    fastest: Option | None = None
    platforms_searched: int = 0


class AdviceResponse(BaseModel):
    """Judgement, not fact. Every number about a flight comes from the search;
    this is a view, and the agent's prompt keeps it labelled as one."""

    ready: bool
    best_airline: str = ""
    stops_advice: str = ""
    recommendation: str = ""
    notes: list[str] = []
    watch_out: str | None = None
    say: str = ""


class SearchResponse(BaseModel):
    """`say` is the answer to read out. Everything else is there so the agent can
    be cross-questioned — which airlines fly this, is there a direct one, what is
    the range — without searching again."""

    say: str
    found: bool
    origin: str
    destination: str
    depart_date: str
    cheapest: Option | None = None
    summary: Summary | None = None
    options: list[Option] = []
    # Opinion about the route: which airline, nonstop or not, what to watch for.
    # Answered from here when asked, rather than searched again or guessed.
    advice: Advice | None = None
    took_seconds: float


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _check(secret: str | None) -> None:
    if not TOOL_SECRET:
        raise HTTPException(500, "FLIGHT_TOOL_SECRET is not configured")
    if secret != TOOL_SECRET:
        raise HTTPException(401, "bad or missing tool secret")


@app.post("/tool/search_flights", response_model=SearchResponse)
async def search_flights(
    body: SearchRequest,
    x_tool_secret: str | None = Header(default=None, alias="X-Tool-Secret"),
) -> SearchResponse:
    _check(x_tool_secret)

    destination = resolve(body.destination)
    origin = resolve(body.origin) if body.origin else DEFAULT_ORIGIN
    # Resolve the route first: the default lead time depends on whether it is
    # domestic, so the date cannot be chosen before the cities are known.
    depart = body.depart_date

    # Nothing matched by name. Before giving up, ask a model to turn what they
    # said into city names — countries, landmarks and descriptions are language,
    # not data. Whatever it suggests is looked up in the same airport dataset, so
    # nothing it invented can reach a search URL.
    options: list[str] = []
    if destination is None:
        destination, options, reason = await resolve_with_model(
            body.destination, call_id="tool"
        )
        log_event("tool.model_resolved", call_id="tool",
                  phrase=body.destination, code=destination, reason=reason)

    if destination is None:
        depart = depart or default_depart_date(None, DEFAULT_ORIGIN)
        if options:
            # Two real cities a long way apart. Choosing for them is not help.
            say = f"{body.destination} — {_spoken_list(options[:3])}. Which one?"
        else:
            say = (f"I couldn't find an airport for {body.destination}. "
                   "Which city should I look at?")
        return SearchResponse(
            say=say, found=False, origin=origin or DEFAULT_ORIGIN,
            destination=body.destination, depart_date=depart, took_seconds=0.0,
        )
    if origin is None:
        origin = DEFAULT_ORIGIN
    depart = depart or default_depart_date(destination, origin)
    if origin == destination:
        return SearchResponse(
            say=f"That's where you're starting from. Where would you like to fly to?",
            found=False, origin=origin, destination=destination,
            depart_date=depart, took_seconds=0.0,
        )

    key = f"{origin}-{destination}-{depart}"
    started = time.monotonic()

    cached = _cache.get(key)
    fresh = cached is not None and (time.monotonic() - _cached_at.get(key, 0)) < CACHE_TTL_S
    if fresh and cached is not None and cached.ok:
        outcome = cached
        log_event("tool.cache_hit", call_id="tool", route=key,
                  age_s=round(time.monotonic() - _cached_at.get(key, 0), 1))
    elif _failed_recently(key):
        # A route that just failed fails fast rather than spending another 45s in
        # the browser. Repeating a timeout is not a retry, it is the same wait
        # again — and the agent asking twice in a conversation is normal.
        log_event("tool.recent_failure", call_id="tool", route=key)
        return SearchResponse(
            say=f"The flight sites still aren't responding for "
                f"{spoken_name(destination)}. Try another destination?",
            found=False, origin=origin, destination=destination,
            depart_date=depart, took_seconds=round(time.monotonic() - started, 1),
        )
    else:
        try:
            outcome = await asyncio.wait_for(
                run_search(destination, call_id="tool", origin=origin, depart_date=depart),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            _failures[key] = time.monotonic()
            return SearchResponse(
                say=f"The flight sites are being slow for {spoken_name(destination)}. "
                    "Want me to try somewhere else?",
                found=False, origin=origin, destination=destination,
                depart_date=depart, took_seconds=round(time.monotonic() - started, 1),
            )
        if outcome.ok:
            # Only successes are cached. Caching a failure would lock the route
            # out for the whole ten-minute window over one bad moment.
            _cache.put(key, outcome)
            _cached_at[key] = time.monotonic()
        else:
            _failures[key] = time.monotonic()

    # Advice is started here and deliberately not awaited. It takes ~13s — it is
    # a thinking model reading twelve options and forming a view — and the first
    # answer must not wait for it. By the time someone has heard the price and
    # asked "which airline is best", it is long since ready, and route_advice
    # returns it instantly.
    if outcome.ok and key not in _advice:
        _advice[key] = asyncio.create_task(advise(outcome, call_id="tool"))

    rows = [_row(o) for o in outcome.options]

    # Published for the phone to pick up. Set before returning, so by the time the
    # agent has finished saying the price the card already has the rows.
    if outcome.ok:
        _latest.clear()
        _latest.update({
            "route": key,
            "origin": origin,
            "destination": destination,
            "destination_city": spoken_name(destination) or destination,
            "depart_date": depart,
            "searched_at": time.time(),
            # Every row, for the card. It scrolls, and a flight missing from the
            # screen because of a number I picked is worse than a long list.
            "options": rows,
        })
    low_high = outcome.price_range
    direct = outcome.direct
    return SearchResponse(
        say=to_sentence(outcome),
        found=outcome.ok,
        origin=origin,
        destination=destination,
        depart_date=depart,
        cheapest=_row(outcome.cheapest) if outcome.cheapest else None,
        summary=Summary(
            total_found=outcome.total_options,
            price_low_inr=low_high[0] if low_high else None,
            price_high_inr=low_high[1] if low_high else None,
            airlines=outcome.airlines,
            direct_available=bool(direct),
            direct_count=len(direct),
            cheapest_direct=_row(direct[0]) if direct else None,
            fastest=_row(outcome.fastest) if outcome.fastest else None,
            platforms_searched=outcome.platforms_seen,
        ) if outcome.ok else None,
        # The model's cap stays. Ninety rows in a prompt is expensive and an agent
        # reading them out is unusable — twelve is enough to be cross-questioned
        # about, and the summary above is computed over all of them, so "which
        # airlines fly this" is answered from the whole search rather than a
        # twelfth of it.
        options=rows[:MODEL_LIMIT],
        took_seconds=round(time.monotonic() - started, 1),
    )


def _spoken_list(codes: list[str]) -> str:
    """Airport codes as a list a person would actually say.

    Two things this gets right that the obvious version does not. `" or ".join`
    produces "Bangkok or Phuket or Chiang Mai", which nobody says; and two items
    take no comma at all — "Tokyo, or Osaka" reads as a stumble.

    Names come from spoken_name, so the curated ones win: the dataset calls Bali
    "Denpasar-Bali Island" and Kochi "Cochin", and neither is a word anyone uses.
    """
    cities = [spoken_name(c) for c in codes]
    cities = [c for c in cities if c]
    if not cities:
        return ""
    if len(cities) == 1:
        return cities[0]
    if len(cities) == 2:
        return f"{cities[0]} or {cities[1]}"
    return ", ".join(cities[:-1]) + f", or {cities[-1]}"


class LatestWeather(BaseModel):
    city: str = ""
    condition: str = ""
    now_c: int | None = None
    fetched_at: float = 0.0
    days: list[WeatherDay] = []


@app.get("/session/weather", response_model=LatestWeather)
async def latest_weather() -> LatestWeather:
    """The last forecast, for the card. Unauthenticated for the same reason as
    /session/latest: it is published weather, and the secret exists to protect the
    browser, not the numbers."""
    if not _weather:
        return LatestWeather()
    data = dict(_weather)
    return LatestWeather(
        city=data.get("city", ""),
        condition=data.get("condition", ""),
        now_c=data.get("now_c"),
        fetched_at=data.get("fetched_at", 0.0),
        days=[WeatherDay(**d) for d in data.get("days", [])],
    )


class LatestResponse(BaseModel):
    """What the phone renders. Rows only — no advice, no summary: the card shows
    flights, and everything else is spoken."""

    route: str = ""
    origin: str = ""
    destination: str = ""
    destination_city: str = ""
    depart_date: str = ""
    searched_at: float = 0.0
    options: list[Option] = []


@app.get("/session/latest", response_model=LatestResponse)
async def latest() -> LatestResponse:
    """The last search, so the phone can show it the moment it lands.

    Polled rather than pushed. The alternative is the agent telling the phone over
    its data channel, which is how the *filtering* works — but results should
    appear even if that channel is silent, and a poll cannot fail in a way that
    leaves the screen empty while the agent is talking about prices.
    """
    # Deliberately unauthenticated, unlike every other endpoint here.
    #
    # It returns published flight prices — nothing private, and nothing that
    # costs anything to serve. The secret exists to stop strangers driving a real
    # Chrome on someone's laptop, and that protection stays on the search. Putting
    # it here too would mean shipping the secret inside the iOS bundle, which is
    # the same "key in an app binary" problem in a smaller costume.
    if not _latest:
        return LatestResponse()
    return LatestResponse(**_latest)


class WeatherRequest(BaseModel):
    destination: str = Field(description="City as spoken")
    day: str | None = Field(
        default=None,
        description="A weekday ('Saturday') or ISO date for one day only. Omit for the week.",
    )


class WeatherDay(BaseModel):
    date: str
    weekday: str
    high_c: int
    low_c: int
    condition: str
    icon: str


class WeatherResponse(BaseModel):
    """`say` is the answer to read out; the rest is what the card draws."""

    say: str
    found: bool
    city: str = ""
    condition: str = ""
    now_c: int | None = None
    days: list[WeatherDay] = []


@app.post("/tool/weather", response_model=WeatherResponse)
async def weather(
    body: WeatherRequest,
    x_tool_secret: str | None = Header(default=None, alias="X-Tool-Secret"),
) -> WeatherResponse:
    """Weather at the destination, for the week or for one day.

    Open-Meteo, no API key, keyed off the airport's own coordinates and timezone —
    so anywhere the agent can price flights to is somewhere it can forecast, with
    no second table to keep in step.
    """
    _check(x_tool_secret)
    destination = resolve(body.destination)
    if destination is None:
        return WeatherResponse(
            say=f"I'm not sure where {body.destination} is. Which city?",
            found=False,
        )

    now = time.monotonic()
    cached = _weather_cache.get(destination)
    if cached and (now - cached[0]) < WEATHER_TTL_S:
        forecast = cached[1]
        log_event("tool.weather_cache_hit", call_id="tool", code=destination)
    else:
        forecast = await forecast_for(destination, call_id="tool")
        if forecast.ok:
            _weather_cache[destination] = (now, forecast)

    if forecast.ok:
        # Published for the card. Set whether they asked for a day or the week:
        # the card always shows the week, and the spoken answer narrows.
        _weather.clear()
        _weather.update({**forecast.as_dict(), "fetched_at": time.time()})

    return WeatherResponse(
        say=forecast.say(body.day),
        found=forecast.ok,
        city=forecast.city,
        condition=forecast.condition,
        now_c=forecast.now_c,
        days=[WeatherDay(**d) for d in forecast.as_dict()["days"]],
    )


class CityNoteRequest(BaseModel):
    destination: str = Field(description="City as spoken")


class CityNoteResponse(BaseModel):
    """One sentence to say while the search runs. Empty when we have nothing
    worth saying — silence beats a confident invention about somewhere the
    person is about to fly."""

    note: str = ""
    city: str = ""


@app.post("/tool/city_note", response_model=CityNoteResponse)
async def city_note(
    body: CityNoteRequest,
    x_tool_secret: str | None = Header(default=None, alias="X-Tool-Secret"),
) -> CityNoteResponse:
    """Something worth hearing while the flight search runs.

    Its own tool, called before the search, because it has to be *said* during the
    wait rather than after it — a note that arrives with the prices has missed the
    silence it existed to fill. Around a second cold and instant once cached.
    """
    _check(x_tool_secret)
    destination = resolve(body.destination)
    if destination is None:
        return CityNoteResponse()
    note = await NOTES.note_for(destination, call_id="tool")
    return CityNoteResponse(note=note, city=spoken_name(destination) or body.destination)


class AdviceRequest(BaseModel):
    destination: str = Field(description="City as spoken, the same one just searched")
    origin: str | None = None
    depart_date: str | None = None


@app.post("/tool/route_advice", response_model=AdviceResponse)
async def route_advice(
    body: AdviceRequest,
    x_tool_secret: str | None = Header(default=None, alias="X-Tool-Secret"),
) -> AdviceResponse:
    """What to actually pick on a route that has already been searched.

    Separate from the search because it is separate work: the search returns what
    exists, this returns a view about it, and only the first is worth making
    someone wait for.
    """
    _check(x_tool_secret)

    destination = resolve(body.destination)
    origin = (resolve(body.origin) if body.origin else DEFAULT_ORIGIN) or DEFAULT_ORIGIN
    if destination is None:
        return AdviceResponse(ready=False, say="Search that route first and I'll have a view on it.")
    depart = body.depart_date or default_depart_date(destination, origin)
    key = f"{origin}-{destination}-{depart}"

    task = _advice.get(key)
    if task is None:
        # Nothing searched, so nothing to have an opinion about. Deliberately not
        # searching here: it would turn one question into a fifteen-second wait.
        return AdviceResponse(
            ready=False,
            say=f"Let me look up {spoken_name(destination)} flights first.",
        )

    try:
        advice = await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return AdviceResponse(ready=False, say="I don't have a view on that one yet.")

    if advice.empty:
        return AdviceResponse(ready=False, say="Nothing particular stands out on that route.")

    log_event("tool.advice_served", call_id="tool", route=key)
    return AdviceResponse(
        ready=True,
        best_airline=advice.best_airline,
        stops_advice=advice.stops_advice,
        recommendation=advice.recommendation,
        notes=list(advice.notes),
        watch_out=advice.watch_out,
        say=advice.recommendation,
    )


def _row(option: Any) -> Option:
    return Option(
        carrier=option.carrier,
        price_inr=option.price,
        stops=option.stops,
        platform=option.platform,
        duration_min=option.duration_min,
        departs=option.depart_spoken,
        arrives=option.arrive_spoken,
        departs_clock=option.depart_clock,
        arrives_clock=option.arrive_clock,
        airline_code=option.airline_code,
    )
