"""Fast-path wake-name detection on interim transcripts (spec section 5.1a).

Runs locally on every interim: no model, no network. The measured interim lag is
~500ms and the finalize lag ~1700ms (see EXECUTION_PLAN Phase 2), so this path is
the only way a direct address gets a response that feels like an answer rather
than a delayed reaction.

Two things this module is careful about, both learned from how interims actually
behave rather than from how they are described:

**Interims are cumulative and get revised.** A single spoken phrase arrives as
"hey", "hey co", "hey copilot", "hey copilot find us". A matcher that just
searches each interim fires four times for one utterance. So firing is tracked
per speech segment and reset when the FINAL arrives.

**STT breaks the name apart.** "copilot" comes back as "co pilot", "co-pilot",
"Copilot," or "copilots". Matching is therefore done over joined word windows
with a small edit-distance tolerance, not against a fixed list of spellings.

Interpretation note on the spec's "wake name **plus** a travel-keyword gate":
implemented as two separate signals, not a conjunction. Requiring a travel word
alongside the name would mean a bare "hey copilot?" does not fire — and section
5.1a is explicit that this path "may only ADD a trigger, never suppress one".
So the name alone is direct address; travel keywords alone only warm the
prefetch, and never fire. Deciding intent from travel words is section 5.1b's
job, at its own confidence bar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Default wake name. Configurable per deployment (spec section 5.1a).
DEFAULT_WAKE_NAME = "copilot"

# Longest wake name we will try to reassemble from split words: "co pilot" is
# two, and a three-word window covers a name STT has fragmented badly.
MAX_WORD_WINDOW = 3

# Edit distance tolerated on a candidate. Scaled by length so short words are
# matched exactly — at distance 1, "copilot" would swallow "cockpit"-like noise
# if we allowed it on 4-letter tokens.
def _allowed_distance(target: str) -> int:
    return 1 if len(target) >= 6 else 0


# Travel words that warm a prefetch. Never fire a trigger on their own.
TRAVEL_KEYWORDS = frozenset({
    "flight", "flights", "fly", "flying", "flew", "airfare", "fare", "fares",
    "ticket", "tickets", "book", "booking", "trip", "travel", "holiday",
    "vacation", "itinerary", "layover", "nonstop", "direct", "airline",
    "airport", "depart", "departure", "return", "onward", "visa", "hotel",
})

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def _levenshtein_within(a: str, b: str, limit: int) -> bool:
    """True if edit distance between a and b is <= limit.

    Hand-rolled to keep the fast path dependency-free, and bailing out early on
    the length gap makes the common case (no match) nearly free.
    """
    if abs(len(a) - len(b)) > limit:
        return False
    if a == b:
        return True
    if limit == 0:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (ca != cb),  # substitution
            ))
        # Every remaining path costs at least this much.
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


@dataclass
class WakeMatch:
    matched: bool
    matched_text: str = ""
    travel_keywords: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.matched


class WakeWordMatcher:
    """Stateless matching of one wake name against a piece of text."""

    def __init__(self, wake_name: str = DEFAULT_WAKE_NAME) -> None:
        self.wake_name = wake_name
        # The name with separators removed, which is what a joined word window
        # is compared against: "co pilot" and "co-pilot" both reduce to this.
        self._target = normalize(wake_name).replace(" ", "")
        if not self._target:
            raise ValueError("wake_name must contain at least one word character")
        self._distance = _allowed_distance(self._target)

    def match(self, text: str) -> WakeMatch:
        words = normalize(text).split()
        found = ""
        for size in range(1, MAX_WORD_WINDOW + 1):
            for i in range(len(words) - size + 1):
                candidate = "".join(words[i : i + size])
                # A plural or possessive leftover ("copilots") should still hit.
                trimmed = candidate[:-1] if candidate.endswith("s") else candidate
                if (
                    _levenshtein_within(candidate, self._target, self._distance)
                    or _levenshtein_within(trimmed, self._target, self._distance)
                ):
                    found = " ".join(words[i : i + size])
                    break
            if found:
                break

        keywords = tuple(w for w in words if w in TRAVEL_KEYWORDS)
        return WakeMatch(matched=bool(found), matched_text=found, travel_keywords=keywords)


@dataclass
class FastPath:
    """Per-call wake detection across the interim stream of several speakers.

    Owns the segment bookkeeping that stops one spoken phrase from firing once
    per interim. Each speaker is tracked separately: two people can be
    mid-utterance at the same time, and they are separate segments.
    """

    matcher: WakeWordMatcher = field(default_factory=WakeWordMatcher)
    _fired_this_segment: set[str] = field(default_factory=set)
    _warmed_this_segment: set[str] = field(default_factory=set)

    def on_interim(self, speaker_id: str, text: str) -> WakeMatch | None:
        """Returns a match the first time a segment earns one, else None.

        None means "nothing new to act on" — either no match, or a match already
        reported for this speech segment.
        """
        result = self.matcher.match(text)
        if result.matched and speaker_id not in self._fired_this_segment:
            self._fired_this_segment.add(speaker_id)
            return result
        return None

    def should_warm(self, speaker_id: str, text: str) -> tuple[str, ...]:
        """Travel keywords worth starting a speculative prefetch on.

        Reported once per segment, and never a trigger: a prefetch is cheap and
        discardable, so it runs on weak evidence that would not justify acting.
        """
        if speaker_id in self._warmed_this_segment:
            return ()
        keywords = self.matcher.match(text).travel_keywords
        if keywords:
            self._warmed_this_segment.add(speaker_id)
        return keywords

    def on_final(self, speaker_id: str) -> None:
        """A finalized utterance closes the segment; the next interim starts a new one."""
        self._fired_this_segment.discard(speaker_id)
        self._warmed_this_segment.discard(speaker_id)
