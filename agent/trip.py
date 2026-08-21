"""What the two of them are actually planning, accumulated across the call.

Spec section 4.3. This is the piece that makes the agent ambient rather than
command-driven: nobody says "hey copilot, find flights from Bangalore to Bali on
the second week of December under thirty thousand, direct only". They say five
separate things over two minutes, and the request only exists in aggregate.

Three properties this type exists to guarantee.

**One utterance cannot decide anything.** A live call transcribed a sentence as
"Fine. Flies to Srinagar." — and Srinagar is a real airport in our table. The only
thing that stopped a confident search for the wrong city was a classifier
happening to judge it unaddressed. So every field here is *pending* until it is
corroborated: mentioned again, or asserted with high confidence. Mishearings do
not repeat; real intentions do.

**Changing your mind is normal and must be cheap.** "Maybe Bali… actually
Thailand" has to land on Thailand without the agent having committed to Bali out
loud. Hence pending/confirmed rather than last-write-wins: a new value has to earn
its place, and until it does the old one is not destroyed either.

**A place name is not an intention.** Reminiscing about a trip to Goa mentions
Goa. `intent_strength` is carried separately from the fields, so the tracker can
hold a complete route it is not yet willing to act on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from .places import resolve, spoken_name

# A single mention asserted at or above this confidence is taken as confirmed
# without waiting for corroboration — someone saying "we're going to Bali on the
# tenth" is not a mishearing.
HIGH_CONFIDENCE = 0.90
# Two independent mentions confirm a value regardless of confidence.
CORROBORATION_NEEDED = 2
# How long the route must stop changing before the agent is willing to answer.
SETTLE_SECONDS = 4.0


@dataclass(frozen=True)
class Candidate:
    value: Any
    mentions: int = 0
    confidence: float = 0.0
    last_at: float = 0.0

    @property
    def effective_mentions(self) -> int:
        """Mentions, with a confident assertion counting double.

        Without this, someone changing their mind clearly and once ("actually,
        let us do Dubai") loses permanently to a destination that happened to be
        mentioned twice earlier. That is not caution, it is being unable to hear a
        decision. One high-confidence statement is worth about as much as two
        passing references."""
        return self.mentions + (1 if self.confidence >= HIGH_CONFIDENCE else 0)

    @property
    def support(self) -> tuple[int, float]:
        """Ordering key: weight of evidence first, then recency.

        Mentions dominate raw confidence, because two independent mentions are
        stronger evidence than one transcript that happened to sound sure. Ties
        break toward the most recent, so a genuine change of mind lands.
        """
        return (self.effective_mentions, self.last_at)


@dataclass(frozen=True)
class Field:
    """One extracted value — as a tally of competing candidates, not a slot.

    A slot was the first design and it was wrong in a way the first trace caught
    immediately: "Bali" was said twice with a mishearing ("Srinagar") in between,
    and each write overwrote the last, so Bali's two mentions never accumulated
    and nothing was ever confirmed.

    Keeping every candidate's support separately means interleaved noise cannot
    erase real evidence. Bali at two mentions beats Srinagar at one, whatever
    order they arrived in.
    """

    candidates: tuple[Candidate, ...] = ()

    @property
    def best(self) -> Candidate | None:
        return max(self.candidates, key=lambda c: c.support, default=None)

    @property
    def value(self) -> Any:
        best = self.best
        return best.value if best else None

    @property
    def confidence(self) -> float:
        best = self.best
        return best.confidence if best else 0.0

    @property
    def mentions(self) -> int:
        best = self.best
        return best.mentions if best else 0

    @property
    def confirmed(self) -> bool:
        best = self.best
        if best is None:
            return False
        return (
            best.mentions >= CORROBORATION_NEEDED
            or best.confidence >= HIGH_CONFIDENCE
        )

    @property
    def usable(self) -> bool:
        return self.confirmed and self.value is not None

    @property
    def contested(self) -> bool:
        """More than one candidate with real support — they are still deciding."""
        supported = [c for c in self.candidates if c.mentions >= 1]
        return len(supported) > 1

    def add(self, value: Any, confidence: float, now: float) -> Field:
        updated: list[Candidate] = []
        seen = False
        for c in self.candidates:
            if c.value == value:
                seen = True
                updated.append(Candidate(
                    value=value,
                    mentions=c.mentions + 1,
                    confidence=max(c.confidence, confidence),
                    last_at=now,
                ))
            else:
                updated.append(c)
        if not seen:
            updated.append(Candidate(value=value, mentions=1,
                                     confidence=confidence, last_at=now))
        # Keep the tally small; a field with more than a handful of candidates is
        # transcription noise rather than indecision.
        updated.sort(key=lambda c: c.support, reverse=True)
        return Field(candidates=tuple(updated[:4]))


EMPTY = Field()


@dataclass(frozen=True)
class TripContext:
    """The accumulated plan. Immutable; `merge` returns a new one."""

    origin: Field = EMPTY
    destination: Field = EMPTY
    depart_date: Field = EMPTY
    return_date: Field = EMPTY
    budget_inr: Field = EMPTY
    direct_only: Field = EMPTY
    travelers: Field = EMPTY

    # Highest intent strength seen so far, from the extractor. Separate from the
    # fields on purpose: a complete route is not permission to act on it.
    intent_strength: float = 0.0
    # When the route (origin, destination) last changed.
    route_changed_at: float = 0.0

    FIELDS = (
        "origin", "destination", "depart_date", "return_date",
        "budget_inr", "direct_only", "travelers",
    )

    # --- reading -----------------------------------------------------------

    @property
    def route(self) -> tuple[str | None, str | None]:
        return (
            self.origin.value if self.origin.usable else None,
            self.destination.value if self.destination.usable else None,
        )

    def route_key(self, *, default_origin: str | None = None) -> str | None:
        """A stable key for dedupe and cache lookup, or None if not searchable."""
        origin, destination = self.route
        origin = origin or default_origin
        if not origin or not destination or origin == destination:
            return None
        date = self.depart_date.value if self.depart_date.usable else "default"
        return f"{origin}-{destination}-{date}"

    def is_searchable(self, *, default_origin: str | None = None) -> bool:
        """Enough to run a search. Origin may be assumed; destination may not."""
        return self.route_key(default_origin=default_origin) is not None

    def is_settled(self, *, now: float | None = None) -> bool:
        """The route has stopped moving, so acting on it will not be embarrassing.

        Prefetching does not wait for this — a discarded search costs nothing.
        Speaking does.
        """
        now = now if now is not None else time.monotonic()
        return (now - self.route_changed_at) >= SETTLE_SECONDS

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"intent_strength": round(self.intent_strength, 2)}
        for name in self.FIELDS:
            f: Field = getattr(self, name)
            if f.value is not None:
                out[name] = f.value
                if not f.confirmed:
                    out[f"{name}_pending"] = True
        return out

    def render(self) -> str:
        """One line for a log or a prompt. Only confirmed values."""
        bits = []
        origin, destination = self.route
        if origin or destination:
            bits.append(f"{spoken_name(origin) or '?'} -> {spoken_name(destination) or '?'}")
        if self.depart_date.usable:
            bits.append(str(self.depart_date.value))
        if self.budget_inr.usable:
            bits.append(f"under {self.budget_inr.value}")
        if self.direct_only.usable and self.direct_only.value:
            bits.append("direct only")
        return ", ".join(bits) or "(nothing yet)"

    # --- writing -----------------------------------------------------------

    def merge(
        self,
        extracted: dict[str, Any],
        *,
        confidence: float = 0.5,
        now: float | None = None,
    ) -> TripContext:
        """Fold one extraction in. Returns a new context.

        `extracted` holds raw values keyed by field name; None means "not
        mentioned", which is different from "changed to nothing".
        """
        now = now if now is not None else time.monotonic()
        updates: dict[str, Any] = {}

        for name in self.FIELDS:
            incoming = extracted.get(name)
            if incoming is None:
                continue
            if name in ("origin", "destination"):
                # Places are resolved through the curated table, so a city the
                # model invented never reaches a search URL.
                incoming = resolve(str(incoming))
                if incoming is None:
                    continue
            current: Field = getattr(self, name)
            updates[name] = current.add(incoming, confidence, now)

        merged = replace(
            self,
            intent_strength=max(
                self.intent_strength, float(extracted.get("intent_strength") or 0.0)
            ),
            **updates,
        )

        # Route churn is what `is_settled` measures, and only a *confirmed* change
        # counts — pending guesses flickering must not keep resetting the clock.
        if merged.route != self.route:
            merged = replace(merged, route_changed_at=now)
        return merged


@dataclass
class TripTracker:
    """Per-call wrapper: holds the context and notices when it becomes actionable."""

    context: TripContext = field(default_factory=TripContext)
    default_origin: str | None = None
    _last_prefetched: str | None = None
    _last_answered: str | None = None

    def merge(self, extracted: dict[str, Any], *, confidence: float = 0.5,
              now: float | None = None) -> TripContext:
        self.context = self.context.merge(extracted, confidence=confidence, now=now)
        return self.context

    def route_to_prefetch(self) -> str | None:
        """A route worth warming, at most once each.

        Prefetch runs on weak evidence because a discarded search costs nothing —
        except that it does cost something real: the flight aggregators throttle
        repeated identical queries, and one route was blocked during testing by
        exactly this. So each route is warmed once and never again.
        """
        key = self.context.route_key(default_origin=self.default_origin)
        if key is None or key == self._last_prefetched:
            return None
        self._last_prefetched = key
        return key

    def route_to_answer(self, *, now: float | None = None,
                        min_intent: float = 0.6) -> str | None:
        """A route worth speaking about: settled, intended, and not already given."""
        key = self.context.route_key(default_origin=self.default_origin)
        if key is None or key == self._last_answered:
            return None
        if self.context.intent_strength < min_intent:
            return None
        if not self.context.is_settled(now=now):
            return None
        self._last_answered = key
        return key

    def evidence(self, *, now: float | None = None) -> float:
        """How strongly the accumulated plan supports acting out loud.

        Deliberately not the classifier's confidence. That judges one utterance in
        isolation and tops out around 0.85 on ordinary trip talk, so gating speech
        on it means the ambient path can search but can never say anything — which
        is what a live rehearsal did: it found the fare and stayed silent.

        What this path actually knows is stronger than any single sentence: the
        destination was corroborated across separate utterances, the route stopped
        moving, and the model rated the conversation as real planning rather than
        reminiscing. Three independent signals agreeing is better evidence than one
        transcript sounding confident, so it is scored on its own terms.
        """
        origin, destination = self.context.route
        if not destination:
            return 0.0

        score = self.context.intent_strength
        # Corroboration is the part a single mishearing cannot fake.
        score += 0.10 * min(2, max(0, self.context.destination.mentions - 1))
        if self.context.is_settled(now=now):
            score += 0.10
        if self.context.origin.usable:
            score += 0.05
        if self.context.depart_date.usable:
            score += 0.05
        # Still deciding between destinations is a reason to stay quiet.
        if self.context.destination.contested:
            score -= 0.15
        # Rounded, because this is a coarse judgement and not a precise quantity.
        # Unrounded, 0.7 + 0.1 + 0.1 is 0.8999999999999999, which silently failed
        # a >= 0.9 gate — the exact case the weights were chosen to let through.
        return round(max(0.0, min(0.99, score)), 2)

    def parts(self) -> tuple[str | None, str | None]:
        origin, destination = self.context.route
        return origin or self.default_origin, destination
