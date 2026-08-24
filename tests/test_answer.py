"""Static unit tests for retrieval and answering.

THE PATTERN: split the system into deterministic parts and one network edge, then
test the deterministic parts exhaustively and stub the edge.

    deterministic (tested here, no DB, no network, no API key)
        retrieve.query_terms / to_or_query   query construction
        chunk.deep_link                      citation URL
        answer.build_prompt                  prompt assembly
        answer.should_abstain                the refuse-to-answer rule
        Answer.cited / Answer.render         which sources get shown

    network edge (stubbed)
        answer.complete                      the single call to the model

What is NOT here: any assertion about the model's wording. Model output is
probabilistic — asserting on it makes a test that fails for reasons unrelated to
your code. Judging answer quality is an *eval* (evals/golden.md), run on demand
and tracked as a score, not a gate on every commit.
"""

from decimal import Decimal

import pytest

from compendium import answer, retrieve
from compendium.corpus.chunk import deep_link


def hit(
    n: int, *, title="A Talk", matched=3, start=421.0, kind="auto", source=None
) -> retrieve.Hit:
    """A Hit built by hand. No database needed to test everything above the SQL.

    `source` defaults to `n`, so hits are from distinct sources unless a test
    deliberately puts two chunks under the same one (which is what the per-source
    cap and any grouping logic care about).
    """
    return retrieve.Hit(
        chunk_id=n,
        source_id=n if source is None else source,
        source_title=title,
        source_url=f"https://www.youtube.com/watch?v=vid{n:08d}",
        transcript_kind=kind,
        start_ts=Decimal(str(start)),
        end_ts=Decimal(str(start + 60)),
        content=f"excerpt body {n}",
        score=1.0,
        matched_terms=matched,
    )


# ------------------------------------------------------------------ query building


def test_query_terms_drop_low_signal_words():
    terms = retrieve.query_terms("Should I use ECS or a traditional object hierarchy?")
    assert "ecs" in terms and "hierarchy" in terms
    assert "should" not in terms and "use" not in terms


def test_to_or_query_quotes_every_term():
    """Quoting is a safety property: an unquoted term containing tsquery syntax
    would be interpreted as an operator rather than searched for."""
    assert retrieve.to_or_query(["ecs", "hierarchy"]) == "'ecs' | 'hierarchy'"


def test_empty_question_produces_no_query():
    assert retrieve.to_or_query(retrieve.query_terms("the and for")) == ""


# ----------------------------------------------------------------------- citations


def test_deep_link_lands_before_the_claim():
    url = "https://www.youtube.com/watch?v=W3aieHjyNvw"
    assert deep_link(url, Decimal("421.9")) == f"{url}&t=421s"


def test_hit_timestamp_is_human_readable():
    assert hit(1, start=421.0).timestamp == "7:01"
    assert hit(1, start=59.4).timestamp == "0:59"


# ----------------------------------------------------------------------- abstention


def test_abstains_with_no_hits():
    assert answer.should_abstain([]) is True


def test_abstains_when_every_hit_is_a_weak_match():
    """One incidental word in common is not grounds for an answer."""
    assert answer.should_abstain([hit(1, matched=1), hit(2, matched=1)]) is True


def test_answers_when_any_hit_matches_enough_terms():
    assert answer.should_abstain([hit(1, matched=1), hit(2, matched=3)]) is False


# -------------------------------------------------------------------------- prompt


def test_prompt_numbers_excerpts_and_includes_timestamps():
    prompt = answer.build_prompt("why?", [hit(1), hit(2)])
    assert "[1]" in prompt and "[2]" in prompt
    assert "excerpt body 1" in prompt
    assert "7:01" in prompt  # the model can cite the moment, not just the talk
    assert prompt.startswith("Question: why?")


def test_prompt_is_deterministic():
    hits = [hit(1), hit(2)]
    assert answer.build_prompt("q", hits) == answer.build_prompt("q", hits)


# ------------------------------------------------------- which sources are shown


def test_only_cited_sources_are_rendered():
    """Retrieval returns 6, a good answer often uses 1. Listing all 6 implies six
    sources support the claim — a citation that does not hold up."""
    a = answer.Answer(
        question="q", text="ECS separates state from behaviour [1].", hits=[hit(1), hit(2), hit(3)]
    )
    assert [n for n, _ in a.cited] == [1]
    rendered = a.render()
    assert "vid00000001" in rendered
    assert "vid00000002" not in rendered
    assert "2 retrieved excerpts were not cited" in rendered


def test_marker_out_of_range_is_ignored():
    """A model that hallucinates [9] must not crash the renderer or invent a link."""
    a = answer.Answer(question="q", text="claim [9]", hits=[hit(1)])
    assert a.cited == []


def test_duplicate_markers_collapse():
    a = answer.Answer(question="q", text="x [1] y [1] z [2]", hits=[hit(1), hit(2)])
    assert [n for n, _ in a.cited] == [1, 2]


# --------------------------------------------------------- the stubbed network edge


async def test_ask_abstains_without_calling_the_model(monkeypatch):
    """Abstention must be decided BEFORE spending money."""
    called = False

    async def boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("model must not be called when abstaining")

    monkeypatch.setattr(answer, "complete", boom)

    async def no_hits(question, limit=6, *, user=None, **kw):
        return [], retrieve.TermReport([], [("game", 538)], 1276)

    monkeypatch.setattr(answer.retrieve, "search_traced", no_hits)

    result = await answer.ask("what is the capital of France?")
    assert result.abstained is True
    assert result.hits == []
    assert called is False


async def test_ask_wires_retrieval_into_the_answer(monkeypatch):
    """The contract, not the wording: hits reach the prompt, tokens reach the Answer."""
    seen: dict = {}

    async def fake_search(question, limit=6, *, user=None, **kw):
        seen["user"] = user
        hits = [hit(1, title="Overwatch Gameplay Architecture and Netcode"), hit(2)]
        return hits, retrieve.TermReport(["ecs", "hierarchy"], [], 1276)

    async def fake_complete(prompt, *, hits=0):
        seen["prompt"] = prompt
        seen["hits"] = hits
        return "ECS separates state from behaviour [1].", 3411, 300

    monkeypatch.setattr(answer.retrieve, "search_traced", fake_search)
    monkeypatch.setattr(answer, "complete", fake_complete)

    result = await answer.ask("Should I use ECS?", user="test1")
    # Tenancy must reach retrieval. If this kwarg is ever dropped, every user
    # silently searches only the shared library and their own sources vanish.
    assert seen["user"] == "test1"
    assert "Overwatch" in seen["prompt"]
    assert seen["hits"] == 2
    assert result.tokens_in == 3411 and result.tokens_out == 300
    assert [n for n, _ in result.cited] == [1]


@pytest.mark.parametrize("text,expected", [("no markers at all", []), ("[1] and [2]", [1, 2])])
def test_citation_extraction_cases(text, expected):
    a = answer.Answer(question="q", text=text, hits=[hit(1), hit(2)])
    assert [n for n, _ in a.cited] == expected


# ------------------------------------------------------------------ provenance


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("manual", "(human-verified)"),
        ("auto", "(auto-caption)"),
        (None, None),  # written prose: no caption claim at all
    ],
)
def test_render_labels_provenance_three_ways(kind, expected):
    """A blog post is not a caption.

    The bug this guards: `"manual" if x == "manual" else "auto-caption"` collapses
    three states into two and tells the reader a machine transcribed an article
    somebody typed. Two thirds of the corpus is prose, so it was wrong far more
    often than it was right.
    """
    result = answer.Answer(
        question="q",
        text="Claim [1].",
        hits=[hit(1, kind=kind)],
    )
    rendered = result.render()
    if expected is None:
        assert "auto-caption" not in rendered
        assert "human-verified" not in rendered
    else:
        assert expected in rendered


# ------------------------------------------------------- hybrid abstention


def sim_hit(n: int, *, matched: int, similarity: float | None) -> retrieve.Hit:
    h = hit(n, matched=matched)
    h.similarity = similarity
    h.lexical_rank = 1 if matched else None
    h.vector_rank = 1 if similarity is not None else None
    return h


@pytest.mark.parametrize(
    "matched,similarity,abstains,why",
    [
        (3, 0.60, False, "both signals strong"),
        (3, None, False, "lexical only, no embeddings at all — the old behaviour"),
        (3, 0.10, False, "shares rare vocabulary; semantic disagreement must not veto it"),
        (0, 0.60, False, "no shared words but squarely on topic — the 3D-assets case"),
        (0, 0.38, True, "the nearest miss: game-adjacent but uncovered (indie pricing)"),
        (1, 0.20, True, "one weak word and nothing close by"),
        (0, None, True, "nothing at all"),
    ],
)
def test_abstains_unless_either_signal_clears(matched, similarity, abstains, why):
    """EITHER test may clear the bar, never BOTH-required.

    Requiring both would refuse most well-covered questions: "how do I optimize
    performance from externally imported 3D model assets" shares no vocabulary with
    "draw call batching" and would be refused on the lexical test alone.

    Requiring either is only safe because the similarity floor was measured against
    out-of-domain questions rather than picked. The 0.38 row is the one that matters
    — it is the closest a question about games but outside the corpus gets.
    """
    hits = [sim_hit(i, matched=matched, similarity=similarity) for i in range(1, 4)]
    assert answer.should_abstain(hits) is abstains, why


def test_best_similarity_ignores_lexical_only_hits():
    """A passage found only by full-text has no similarity. Treating None as 0 would
    drag the maximum down and cause a false refusal on a well-covered question."""
    hits = [
        sim_hit(1, matched=2, similarity=None),
        sim_hit(2, matched=0, similarity=0.62),
        sim_hit(3, matched=1, similarity=None),
    ]
    assert retrieve.best_similarity(hits) == pytest.approx(0.62)
    assert retrieve.best_similarity([sim_hit(1, matched=2, similarity=None)]) is None
