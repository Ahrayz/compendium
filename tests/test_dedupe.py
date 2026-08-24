"""Near-duplicate detection.

No database and no network: the whole point of `dedupe` is that it turns text into
a fixed-size fingerprint, so it can be tested on strings.
"""

import pytest

from compendium.corpus import dedupe

ARTICLE = (
    "An entity component system separates identity from data and from behaviour. "
    "Entities are identifiers, components are plain data, and systems iterate over "
    "components in tightly packed arrays. The memory layout is the point: iterating "
    "a contiguous array of components is cache friendly in a way that chasing "
    "pointers through an inheritance hierarchy is not. Archetype implementations "
    "group entities with identical component sets into dense columnar tables. "
) * 3


def reformat(text: str) -> str:
    """The same prose after a round trip through a renderer: different case,
    different punctuation, different whitespace, same words."""
    return text.upper().replace(".", " .").replace(",", " ,").replace(" ", "\n  ")


def test_reformatting_defeats_exact_hashing_but_not_shingles():
    """The reason this module exists rather than a sha256 column.

    A rendered page and its Markdown source differ on nearly every line, and one
    changed byte changes a hash completely — so exact hashing reports "unrelated"
    for two copies of the same document.
    """
    a, b = ARTICLE, reformat(ARTICLE)
    assert a != b
    assert hash(a) != hash(b)

    sa, ca = dedupe.sketch(a)
    sb, cb = dedupe.sketch(b)
    assert dedupe.containment(sa, ca, sb, cb) > 0.95


def test_containment_is_asymmetric():
    """A short article inside a long book is contained in it; the book is not
    contained in the article. Symmetry here would mean the short source never
    loses its slot, which is backwards."""
    short, long = ARTICLE[:600], ARTICLE + " Systems may also be scheduled in parallel. " * 40
    s_s, s_c = dedupe.sketch(short)
    l_s, l_c = dedupe.sketch(long)
    assert dedupe.containment(s_s, s_c, l_s, l_c) > dedupe.containment(l_s, l_c, s_s, s_c)


def test_unrelated_text_scores_near_zero():
    other = (
        "Rollback netcode predicts remote inputs and re-simulates the last few "
        "frames when a prediction turns out wrong. Deterministic lockstep instead "
        "waits for every player's input before advancing the simulation. "
    ) * 3
    a_s, a_c = dedupe.sketch(ARTICLE)
    o_s, o_c = dedupe.sketch(other)
    assert dedupe.containment(a_s, a_c, o_s, o_c) < 0.1


def test_sketch_size_is_bounded_by_document_length():
    """The property that makes this storable: a document ten times longer does not
    produce a fingerprint ten times bigger.

    The filler has to be *varied* — shingles are a set, so repeating the same
    paragraph fifty times adds no new shingles at all.
    """
    long_text = ARTICLE + " ".join(
        f"System number {i} iterates archetype {i} across chunk boundary {i}." for i in range(2000)
    )
    small, small_count = dedupe.sketch(ARTICLE)
    large, count = dedupe.sketch(long_text)
    assert count > small_count * 10  # genuinely a much larger document
    assert count > dedupe.SKETCH_SIZE
    assert len(small) <= dedupe.SKETCH_SIZE
    assert len(large) <= dedupe.SKETCH_SIZE  # ...but the fingerprint did not grow


def test_sketch_values_fit_a_signed_bigint():
    """Shingles are stored in a BIGINT[] column. A 64th bit would overflow it, and
    Postgres would reject the insert at write time rather than at review time."""
    values, _ = dedupe.sketch(ARTICLE)
    assert all(0 <= v <= 2**63 - 1 for v in values)


@pytest.mark.parametrize("text", ["", "   ", "short"])
def test_degenerate_input_does_not_raise(text):
    """Sources shorter than one shingle exist — a link-only stub, an empty fetch.
    They must not crash ingestion or match everything."""
    s, c = dedupe.sketch(text)
    assert c >= 1
    a_s, a_c = dedupe.sketch(ARTICLE)
    assert dedupe.containment(s, c, a_s, a_c) < dedupe.DUPLICATE_THRESHOLD


def test_is_duplicate_checks_both_directions():
    """Ingesting the long version after the short one must be caught as readily as
    the reverse, or detection depends on manifest order."""
    short, long = ARTICLE, ARTICLE * 4
    s_s, s_c = dedupe.sketch(short)
    l_s, l_c = dedupe.sketch(long)
    assert dedupe.is_duplicate(s_s, s_c, l_s, l_c)
    assert dedupe.is_duplicate(l_s, l_c, s_s, s_c)
