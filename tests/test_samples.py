"""The recorded example answers, and the boundary around them.

These tests exist because the cache is a claim about honesty, not a performance
feature. A product whose whole argument is "you can see what actually happened"
must never replay a recording while implying it ran just now, and must never serve
a recording for a question nobody asked.
"""

import json

import pytest

from compendium import sample_answers


@pytest.mark.parametrize(
    "given,expected",
    [
        ("Should I use ECS?", "should i use ecs?"),
        ("  Should   I use ECS? ", "should i use ecs?"),
        ("SHOULD I USE ECS?", "should i use ecs?"),
    ],
)
def test_normalise_ignores_only_case_and_whitespace(given, expected):
    """Case and spacing are not meaningful differences between two askings of the
    same question. Anything else is."""
    assert sample_answers.normalise(given) == expected


def test_every_recording_declares_itself_cached():
    """`cached: true` is what the UI reads to say "saved run". A recording written
    without it would render as though it had just executed — the one failure mode
    this feature must not have."""
    assert sample_answers.entries(), "no samples recorded"
    for question, user in sample_answers.entries():
        payload = sample_answers.lookup(question, user)
        assert payload is not None
        assert payload["cached"] is True, f"{question!r} is not marked cached"


def test_recordings_carry_real_evidence():
    """A sample must be a capture of a real run, not written prose: it has to have
    the passages and citations that produced it. Without those the trace panel
    would be empty and the answer unverifiable — worse than no example."""
    for question, user in sample_answers.entries():
        p = sample_answers.lookup(question, user)
        assert p["citations"], f"{question!r} cites nothing"
        assert p["passages"], f"{question!r} has no retrieved passages"
        assert p["abstained"] is False
        assert p["cost_usd"] and p["cost_usd"] > 0, "a real run costs something"
        assert any(c["url"].startswith("http") for c in p["citations"])


def test_a_changed_question_misses_the_cache():
    """One edited character must run live. If near-misses matched, the system would
    answer a question the reader did not ask — precisely what it exists to refuse."""
    question, user = sample_answers.entries()[0]
    assert sample_answers.lookup(question, user) is not None
    assert sample_answers.lookup(question + " for a small team", user) is None
    assert sample_answers.lookup(question.replace("?", " really?"), user) is None


def test_the_same_question_against_another_corpus_misses():
    """Recordings are keyed on the corpus too. Serving a test1 recording to someone
    searching the shared library would show them citations from sources they cannot
    see — wrong in the most confusing possible way."""
    question, user = sample_answers.entries()[0]
    assert user is not None
    assert sample_answers.lookup(question, None) is None
    assert sample_answers.lookup(question, "someone-else") is None


def test_recordings_are_valid_json_and_round_trip():
    for path in sorted(sample_answers.SAMPLES.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["question"]
        assert "recorded_for_user" in payload
