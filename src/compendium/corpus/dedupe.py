"""Near-duplicate detection between sources.

THE PROBLEM. `sources` is keyed on (owner, url), so the same *document* published
at two URLs is two rows. Real cases already in this corpus:

    flecs.dev/ecs-faq                 and  github.com/SanderMertens/ecs-faq
    http://gameprogrammingpatterns…   and  https://gameprogrammingpatterns…

Both then compete for slots in the same result set, and an answer that cites the
same text twice looks like corroboration when it is one source counted twice.
That is a correctness problem, not a tidiness one.

WHY HASHING THE CONTENT IS NOT ENOUGH. Measured on the ECS FAQ pair (16 vs 17
chunks of the same document, one rendered from HTML, one from Markdown):

    exact chunk hash (sha256)                    3 shared,  jaccard 0.100
    hash after lowercase/strip-punctuation      14 shared,  jaccard 0.737
    8-word shingles                                         jaccard 0.907

Exact hashing misses it almost entirely: a rendered page and its Markdown source
differ in whitespace and punctuation on nearly every line, and one differing byte
changes the hash completely. Hashing is exact-match detection; this is a
near-match problem, so the unit of comparison has to be smaller than the document
and robust to reformatting.

WHAT THIS DOES. Each source becomes a set of hashed 8-word shingles — every
overlapping run of 8 words. Reformatting changes a handful of shingles; the rest
survive, so the sets still overlap heavily.

Keeping every shingle would cost roughly one integer per word (~600k rows for this
corpus, growing with it), so each source stores a bounded **bottom-k sketch**: the
128 numerically smallest shingle hashes, plus the true shingle count. Because the
hash spreads values uniformly, the smallest 128 are an unbiased sample of the whole
set, and — crucially — for any value small enough to be in the sketch, the sketch
alone decides membership. That makes set arithmetic possible from a fixed 128
numbers per source no matter how long the document is.

CONTAINMENT, NOT JACCARD, IS THE QUESTION. Jaccard (shared / combined) punishes a
size difference, so a short article genuinely reprinted inside a long one scores
low. The question worth asking is "is this source already inside that one?" —
containment. On the ECS FAQ pair: jaccard 0.907, containment 0.995.

Measured over all 134 sources: every true duplicate pair scores >= 0.91 and the
highest false pair scores 0.18. The threshold sits in a gap that wide, which is
why 0.80 is a safe constant rather than a tuned one.

WHERE THIS STOPS SCALING. Comparing a new source against every existing one is
O(n) per ingest, O(n^2) for a rebuild — fine at 134 sources, not at 100k. The
standard fix is MinHash with LSH banding, which turns "compare against everything"
into a hash lookup of likely candidates. Not built, because at this size it would
be complexity bought against a load that does not exist. The measurement that
would justify it: ingest wall-time growing linearly with corpus size.
"""

import hashlib
import re

# 8 words. Shorter and common phrases collide across unrelated documents ("at the
# end of the day"); longer and a single edited word destroys too many shingles.
SHINGLE_WORDS = 8

# Bounded storage per source. 128 was enough to reproduce a true containment of
# 0.995 as 0.989 on the ECS FAQ pair; 512 bought 0.996 and four times the storage.
SKETCH_SIZE = 128

# Above this, treat as the same document.
#
# Not tuned — read off a gap. Scanning all 35,532 ordered pairs of 189 sources, only
# six scored above 0.35, and all six were the same ECS FAQ published three ways
# (rendered site, source repo, and the repo's README):
#
#     1.000  repo README  <-> the same README via another URL
#     0.774  rendered site -> README        (the site renders part of the source)
#     0.651  README       -> rendered site
#     ...nothing at all between 0.35 and 0.651...
#
# 0.60 sits inside that empty band, so the constant is insensitive to where exactly
# it is placed — which is the property worth having. If a future corpus fills the
# band in, this stops being a constant and needs a labelled set behind it.
DUPLICATE_THRESHOLD = 0.60

_NOT_WORD = re.compile(r"[^a-z0-9\s]+")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    This is what makes the comparison survive reformatting. It is deliberately
    lossy — `Foo()` and `foo` become the same token — because the question is
    "is this the same prose", not "is this the same code".
    """
    return _SPACE.sub(" ", _NOT_WORD.sub(" ", text.lower())).strip()


def shingles(text: str, k: int = SHINGLE_WORDS) -> set[int]:
    """Every overlapping run of `k` words, hashed to a 63-bit int.

    63 rather than 64 bits so the values fit Postgres BIGINT, which is signed.
    """
    words = normalise(text).split()
    if len(words) < k:
        # Too short to shingle. Hash the whole thing so tiny sources still compare
        # rather than silently matching everything or nothing.
        words = words or [""]
        return {_hash(" ".join(words))}
    return {_hash(" ".join(words[i : i + k])) for i in range(len(words) - k + 1)}


def _hash(s: str) -> int:
    digest = hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1  # 63 bits: fits a signed BIGINT


def sketch(text: str, size: int = SKETCH_SIZE) -> tuple[list[int], int]:
    """Return (bottom-k sketch, true shingle count).

    The count is stored alongside because containment cannot be recovered from the
    sketches alone — see `containment`.
    """
    full = shingles(text)
    return sorted(full)[:size], len(full)


def jaccard(sketch_a: list[int], sketch_b: list[int], size: int = SKETCH_SIZE) -> float:
    """Estimate |A n B| / |A u B| from two bottom-k sketches.

    The estimate is taken over the k smallest values of the *combined* sketches.
    Restricting to that window is what makes it valid: any value small enough to
    appear there would have been kept by either source's own sketch, so presence in
    the sketch is proof of presence in the set, and absence is proof of absence.
    Comparing the raw sketches instead would undercount, because A's 128 smallest
    and B's 128 smallest need not cover the same range.
    """
    a, b = set(sketch_a), set(sketch_b)
    if not a or not b:
        return 0.0
    window = set(sorted(a | b)[:size])
    return len(window & a & b) / len(window)


def containment(
    sketch_a: list[int],
    count_a: int,
    sketch_b: list[int],
    count_b: int,
    size: int = SKETCH_SIZE,
) -> float:
    """What fraction of A also appears in B — "is A already inside B?".

    Derived rather than measured directly: from the Jaccard estimate J and the two
    true set sizes,

        J = |A n B| / |A u B|  and  |A u B| = |A| + |B| - |A n B|
        =>  |A n B| = J(|A| + |B|) / (1 + J)

    so containment = |A n B| / |A|. Asymmetric on purpose: a short article inside a
    long book is contained in it, but not the other way round, and the short one is
    the one that should lose its slot.
    """
    if not count_a:
        return 0.0
    j = jaccard(sketch_a, sketch_b, size)
    if j <= 0:
        return 0.0
    intersection = j * (count_a + count_b) / (1 + j)
    return min(1.0, intersection / count_a)


def is_duplicate(
    sketch_a: list[int],
    count_a: int,
    sketch_b: list[int],
    count_b: int,
    threshold: float = DUPLICATE_THRESHOLD,
) -> bool:
    """True when either source is substantially contained in the other.

    Checked both ways so that ingesting the long version after the short one is
    caught as readily as the reverse.
    """
    return (
        containment(sketch_a, count_a, sketch_b, count_b) >= threshold
        or containment(sketch_b, count_b, sketch_a, count_a) >= threshold
    )
