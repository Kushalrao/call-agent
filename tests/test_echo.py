"""The agent must not hear itself (agent/worker.py echo rejection).

On a live call the agent said "Checking Dubai flights." and five seconds later
transcribed "Checking Dubai flights." as one of the humans. The phones use
`.defaultToSpeaker` because people are looking at the screen, so the agent's own
voice leaves the speaker and returns through the mic. That is one classifier
verdict away from the agent answering itself in a loop.
"""

from __future__ import annotations

from agent.worker import CallAgent


def agent() -> CallAgent:
    return CallAgent("test-call")


def test_the_agent_recognises_its_own_words_coming_back():
    a = agent()
    a.remember_spoken("Checking Dubai flights.")
    assert a.is_own_echo("Checking Dubai flights.")


def test_echo_matching_ignores_case_and_punctuation():
    """STT re-punctuates and re-capitalises, so an exact match is not enough."""
    a = agent()
    a.remember_spoken("Cheapest to Dubai is Air India Express at 25,344, one stop.")
    assert a.is_own_echo("cheapest to dubai is air india express at 25344 one stop")


def test_a_partial_tail_still_counts_as_echo():
    """The echo usually comes back as a fragment rather than the whole sentence."""
    a = agent()
    a.remember_spoken("Cheapest to Singapore is Air India at 19,532, one stop, on Cleartrip.")
    assert a.is_own_echo("is Air India at 19,532 one stop")


def test_a_human_asking_a_question_is_never_echo():
    """The expensive mistake would be discarding real speech."""
    a = agent()
    a.remember_spoken("Checking Dubai flights.")
    for real in [
        "Hey copilot, find me flights to Dubai.",
        "What about Singapore instead?",
        "Under thirty thousand please.",
        "Dubai sounds good.",
    ]:
        assert not a.is_own_echo(real), real


def test_nothing_is_echo_before_the_agent_speaks():
    a = agent()
    assert not a.is_own_echo("Checking Dubai flights.")


def test_empty_text_is_not_echo():
    a = agent()
    a.remember_spoken("Checking Dubai flights.")
    assert not a.is_own_echo("")
    assert not a.is_own_echo("   ")


def test_only_recent_speech_counts():
    """A phrase the agent used two minutes ago must not silence a human who
    happens to say something similar now."""
    import agent.worker as worker

    a = agent()
    a.remember_spoken("Checking Dubai flights.")
    # Age every remembered entry past the window.
    a._spoken = type(a._spoken)(
        ((at - worker.ECHO_WINDOW_S - 1, text) for at, text in a._spoken), maxlen=8
    )
    assert not a.is_own_echo("Checking Dubai flights.")


def test_the_memory_is_bounded():
    a = agent()
    for i in range(50):
        a.remember_spoken(f"utterance number {i}")
    assert len(a._spoken) <= 8
