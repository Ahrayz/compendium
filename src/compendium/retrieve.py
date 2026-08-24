"""Retrieval over the corpus.

Full-text only for now. The `content_tsv` column is GENERATED and GIN-indexed, so
this works today with no embeddings and no OpenAI key. When `embed.py` has run,
vector search joins here and the two get fused — that is why the retrieval path
lives in one table.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from compendium import db
from compendium.corpus import embed
from compendium.corpus.chunk import deep_link

# Terms shorter than this carry no signal and blow up the OR query.
_MIN_TERM = 2
_WORD = re.compile(r"[A-Za-z0-9_+#-]+")

# Not Postgres stopwords (those are dropped during lexization) but words common
# enough in conference talks that ORing them buries the terms that matter.
_LOW_SIGNAL = frozenset(
    [
        "should",
        "would",
        "could",
        "about",
        "what",
        "when",
        "where",
        "which",
        "have",
        "has",
        "had",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "just",
        "make",
        "made",
        "really",
        "going",
        "want",
        "need",
        "know",
        "think",
        "thing",
        "things",
        "people",
        "team",
        "person",
        "good",
        "better",
        "best",
        "use",
        "using",
        "used",
        "lot",
        "bit",
        "way",
        "ways",
        "kind",
        "sort",
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "over",
        "under",
        "you",
        "your",
        "our",
        "are",
        "was",
        "were",
        "but",
        "not",
        "can",
        "will",
        "how",
        "why",
        "who",
        "its",
        "than",
        "then",
    ]
)


@dataclass(slots=True)
class Hit:
    """One retrieved chunk, with everything a citation needs."""

    chunk_id: int
    source_id: int
    source_title: str
    source_url: str
    transcript_kind: str | None
    start_ts: Decimal | None
    end_ts: Decimal | None
    content: str
    score: float
    matched_terms: int = 0

    # Provenance of the retrieval itself. `similarity` is cosine against the
    # question; the ranks are 1-based within each retriever, None if that
    # retriever did not return this chunk at all. The UI turns these into
    # "found by matching words", "found by meaning", or "found by both".
    similarity: float | None = None
    lexical_rank: int | None = None
    vector_rank: int | None = None

    @property
    def citation(self) -> str:
        """Deep link to the moment the claim was made."""
        return deep_link(self.source_url, self.start_ts)

    @property
    def timestamp(self) -> str:
        if self.start_ts is None:
            return "-"
        total = int(self.start_ts)
        return f"{total // 60}:{total % 60:02d}"


def query_terms(question: str) -> list[str]:
    """Significant, de-duplicated lower-case terms from a question."""
    return list(
        dict.fromkeys(
            t.lower()
            for t in _WORD.findall(question)
            if len(t) > _MIN_TERM and t.lower() not in _LOW_SIGNAL
        )
    )


def to_or_query(terms: list[str]) -> str:
    """OR the terms into a tsquery.

    plainto_tsquery ANDs everything, so a whole sentence matches nothing. ORing
    gives recall; ranking then has to do the real work.
    """
    return " | ".join(f"'{t}'" for t in terms)


# A term in more than this share of chunks is describing the corpus, not the
# question. Measured on 1,276 chunks: ecs 10, hierarchy 10, price 2, indie 4 —
# against object 183, much 394, game 538.
MAX_DOC_FREQ = 0.10


@dataclass(slots=True)
class TermReport:
    """Why each query term was kept or dropped. The explanation of an answer —
    or of a refusal — starts here."""

    kept: list[str]
    dropped: list[tuple[str, int]]  # (term, document frequency)
    total_chunks: int

    def render(self) -> str:
        keep = ", ".join(self.kept) or "(none)"
        drop = ", ".join(f"{t}(df={d})" for t, d in self.dropped) or "(none)"
        return f"kept: {keep}\n    dropped: {drop}"


async def analyse_terms(terms: list[str], *, user: str | None = None) -> TermReport:
    """Split terms into evidence and noise, and say why.

    A term in more than MAX_DOC_FREQ of chunks describes the corpus, not the
    question. Returning the dropped terms too is what makes an abstention
    explainable instead of mysterious.
    """
    if not terms:
        return TermReport([], [], 0)
    owners = visible_owners(user)
    # Document frequency is measured over exactly the chunks this request can see.
    # Counting against the whole table would mis-classify a term that is rare in
    # the shared library but everywhere in one user's own notes — and the point of
    # the measure is "rare *here*", where here means this user's view.
    rows = await db.fetch(
        """
        SELECT term,
               (SELECT count(*) FROM chunks c
                  JOIN sources s ON s.id = c.source_id
                 WHERE c.content_tsv @@ to_tsquery('english', term)
                   AND s.owner = ANY(%(owners)s)
                   AND s.duplicate_of IS NULL) AS df
        FROM unnest(%(terms)s::text[]) AS term
        """,
        {"terms": terms, "owners": owners},
    )
    total = (
        await db.fetch(
            "SELECT count(*) AS n FROM chunks c JOIN sources s ON s.id = c.source_id "
            "WHERE s.owner = ANY(%(owners)s) AND s.duplicate_of IS NULL",
            {"owners": owners},
        )
    )[0]["n"] or 1
    kept, dropped = [], []
    for r in rows:
        df = r["df"] or 0
        (kept if df and df / total <= MAX_DOC_FREQ else dropped).append(
            r["term"] if df and df / total <= MAX_DOC_FREQ else (r["term"], df)
        )
    return TermReport(kept, dropped, total)


async def discriminating_terms(terms: list[str], *, user: str | None = None) -> list[str]:
    """Keep only terms rare enough to be evidence.

    This is document frequency doing the job TF-IDF would do for us. Without it,
    "how much should I price my indie game?" matches three terms — game, much,
    price — and looks like a confident hit, when only one of the three carries any
    signal. Cheap to compute against a GIN index, and it is the difference between
    a system that answers everything and one that knows what it does not cover.
    """
    # If every term is common the question is not answerable from this corpus;
    # returning nothing lets the caller abstain rather than answer from noise.
    return (await analyse_terms(terms, user=user)).kept


def visible_owners(user: str | None) -> list[str]:
    """Which corpora this request may read.

    Everyone sees the shared library. A signed-in user additionally sees their own
    sources, and nobody else's. This is the entire tenancy rule, and it is written
    once here rather than repeated in every query — a filter that lives in three
    places is a filter that will be forgotten in one of them.
    """
    return ["public"] if not user or user == "public" else ["public", user]


async def search(question: str, limit: int = 8, *, user: str | None = None) -> list[Hit]:
    """Convenience wrapper. Use `search_traced` when you want the explanation."""
    hits, _ = await search_traced(question, limit, user=user)
    return hits


# At most this many passages from any one source. Without it a single long talk
# fills the whole result set: the ECS question returned 5 of 8 chunks from the
# Overwatch talk, which reads as "one source has the answer" when really it just
# has the most chunks. A cap is the cheapest possible diversity control and it is
# what makes a two-sided answer even possible.
MAX_PER_SOURCE = 2

# Reciprocal-rank-fusion constant. Fusing by RANK rather than by score is the
# whole point: ts_rank_cd and cosine similarity are different units on different
# scales, and normalising them against each other would invent a comparison that
# does not exist. Rank is the one thing both retrievers genuinely agree on the
# meaning of. 60 is the value from the original RRF paper; the ordering is
# insensitive to it, and inventing a different one would imply a tuning exercise
# that never happened.
RRF_K = 60

# A question is "covered semantically" above this cosine similarity. Measured, not
# guessed — the top hit for each question, over this corpus:
#
#     0.631  Should I use ECS or a traditional object hierarchy?      in domain
#     0.527  How do I optimize imported 3D model assets?              in domain
#     ------------------------------------------------ 0.45 sits here
#     0.382  How much should I price my one-off indie game?           out of domain
#     0.235  How do I file taxes as a sole trader in Singapore?       out of domain
#     0.191  What is the best recipe for sourdough bread?             out of domain
#
# The pricing question is the one that matters: it is game-adjacent, so it is the
# nearest thing to a false positive this corpus can produce, and it still sits
# well below the band.
MIN_SIMILARITY = 0.45


async def _lexical(
    tsquery: str, terms: list[str], owners: list[str], limit: int, per_source: int
) -> list[dict]:
    """Rank by cover density scaled by distinct terms matched.

    Multiplying rather than nesting keeps both signals live: `density * matched` is
    "how tightly the rare words cluster here, scaled by how many different rare
    words this passage covers". An earlier version sorted lexicographically —
    `matched DESC, density DESC` — which let *any* two-term match beat *every*
    one-term match, putting a density-0.30 passage above a density-0.70 one.
    """
    return await db.fetch(
        """
        WITH scored AS (
            SELECT c.id, c.content, c.start_ts, c.end_ts, c.source_id,
                   s.title, s.url, s.transcript_kind,
                   (
                       SELECT count(*) FROM unnest(%(terms)s::text[]) AS term
                       WHERE c.content_tsv @@ to_tsquery('english', term)
                   ) AS matched,
                   ts_rank_cd(c.content_tsv, query) * (
                       SELECT count(*) FROM unnest(%(terms)s::text[]) AS term
                       WHERE c.content_tsv @@ to_tsquery('english', term)
                   ) AS score
            FROM chunks c
            JOIN sources s ON s.id = c.source_id,
                 to_tsquery('english', %(q)s) AS query
            WHERE c.content_tsv @@ query
              AND s.owner = ANY(%(owners)s)
              AND s.duplicate_of IS NULL
        )
        SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY source_id ORDER BY score DESC, id) AS rn
            FROM scored
        ) ranked
        WHERE rn <= %(per_source)s
        ORDER BY score DESC, id
        LIMIT %(limit)s
        """,
        {
            "q": tsquery,
            "terms": terms,
            "owners": owners,
            "limit": limit,
            "per_source": per_source,
        },
    )


async def _semantic(vector: str, owners: list[str], limit: int, per_source: int) -> list[dict]:
    """Nearest chunks by cosine distance.

    No vector index: at 3,368 rows a sequential scan is not the bottleneck, and
    building an HNSW index before being able to name the query that degrades would
    be cargo cult. The three questions gating that decision are in schema.py.
    """
    return await db.fetch(
        """
        WITH scored AS (
            SELECT c.id, c.content, c.start_ts, c.end_ts, c.source_id,
                   s.title, s.url, s.transcript_kind,
                   0 AS matched, 0 AS score,
                   1 - (c.embedding <=> %(v)s::vector) AS similarity
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE c.embedding IS NOT NULL
              AND s.owner = ANY(%(owners)s)
              AND s.duplicate_of IS NULL
            ORDER BY c.embedding <=> %(v)s::vector
            LIMIT %(scan)s
        )
        SELECT * FROM (
            SELECT *, row_number() OVER (
                PARTITION BY source_id ORDER BY similarity DESC, id
            ) AS rn
            FROM scored
        ) ranked
        WHERE rn <= %(per_source)s
        ORDER BY similarity DESC, id
        LIMIT %(limit)s
        """,
        # `scan` is the window the cap is applied within. Capping has to happen
        # after ordering by distance but before the final LIMIT, or a long document
        # eats the pool: on one question "Billions of Triangles in Minutes"
        # occupied 5 of the top 24 and the short authoritative docs — Unity's LOD
        # page, the Nanite docs — never became candidates at all.
        {
            "v": vector,
            "owners": owners,
            "limit": limit,
            "per_source": per_source,
            "scan": limit * 8,
        },
    )


async def search_traced(
    question: str,
    limit: int = 8,
    *,
    per_source: int = MAX_PER_SOURCE,
    user: str | None = None,
    semantic: bool = True,
) -> tuple[list[Hit], TermReport]:
    """Retrieve by words and by meaning, then fuse the two rankings.

    WHY BOTH. Lexical search cannot bridge vocabulary. "How do I optimize
    performance from externally imported 3D model assets?" contains none of the
    words the answer is written in — draw calls, LOD, mesh simplification, Nanite —
    so full-text search returned nothing useful for it while semantic search put
    the three right sources in the top three. The reverse also happens: an exact
    term like `ECS` or a version number is precisely what embeddings blur away.

    HOW THEY ARE COMBINED. Reciprocal rank fusion: each chunk scores
    `sum(1 / (RRF_K + rank))` over the retrievers that returned it. Fusing ranks
    rather than scores is deliberate — `ts_rank_cd` and cosine similarity are
    different units on different scales, and normalising one onto the other would
    manufacture a comparison that does not exist. A chunk found by both retrievers
    outranks one found by either, which is the behaviour worth having.

    DEGRADING. With no embeddings, or no OpenAI key, or a failed embedding call,
    this is exactly the lexical search it was before. Semantic search improves
    retrieval; it is not load-bearing for it.
    """
    report = await analyse_terms(query_terms(question), user=user)
    owners = visible_owners(user)
    tsquery = to_or_query(report.kept)

    vector = await embed.embed_query(question) if semantic else None
    if not tsquery and vector is None:
        return [], report

    # Over-fetch from each retriever: fusion and the per-source cap both discard
    # rows, so asking for exactly `limit` from each would starve the final list.
    pool = max(limit * 4, 24)
    # The cap is applied to each retriever's candidates too, not only to the final
    # list: capping at the end limits what is *shown* without changing what was
    # *considered*, so a verbose source still crowds out everything else.
    #
    # But the candidate cap is deliberately LOOSER than the output cap. The two
    # retrievers routinely pick different passages from the same document, and RRF
    # only rewards agreement when it sees the same chunk twice. Capped at the output
    # width, the ECS FAQ ranked first in both retrievers on different chunks, neither
    # got the agreement bonus, and a chunk ranked third in both displaced it out of
    # the results entirely. At twice the width the overlap survives and it returns to
    # first. Measured at 2/3/4/6; 4 was where it recovered and 6 added nothing.
    pool_per_source = per_source * 2
    lex = await _lexical(tsquery, report.kept, owners, pool, pool_per_source) if tsquery else []
    vec = await _semantic(vector, owners, pool, pool_per_source) if vector is not None else []

    fused: dict[int, dict] = {}
    for rank, row in enumerate(lex, 1):
        fused[row["id"]] = {**row, "rrf": 1 / (RRF_K + rank), "lex_rank": rank, "vec_rank": None}
    for rank, row in enumerate(vec, 1):
        existing = fused.get(row["id"])
        if existing:
            existing["rrf"] += 1 / (RRF_K + rank)
            existing["vec_rank"] = rank
            existing["similarity"] = row["similarity"]
        else:
            fused[row["id"]] = {
                **row,
                "rrf": 1 / (RRF_K + rank),
                "lex_rank": None,
                "vec_rank": rank,
            }

    ordered = sorted(fused.values(), key=lambda r: (-r["rrf"], r["id"]))

    # At most `per_source` passages from any one source. Without it a single long
    # talk fills the whole result set and a one-sided answer looks corroborated.
    kept: list[dict] = []
    seen: dict[int, int] = {}
    for row in ordered:
        sid = int(row["source_id"])
        if seen.get(sid, 0) >= per_source:
            continue
        seen[sid] = seen.get(sid, 0) + 1
        kept.append(row)
        if len(kept) >= limit:
            break

    return [
        Hit(
            chunk_id=r["id"],
            source_id=r["source_id"],
            source_title=r["title"],
            source_url=r["url"],
            transcript_kind=r["transcript_kind"],
            start_ts=r["start_ts"],
            end_ts=r["end_ts"],
            content=r["content"],
            score=float(r["rrf"]),
            matched_terms=int(r["matched"] or 0),
            similarity=float(r["similarity"]) if r.get("similarity") is not None else None,
            lexical_rank=r["lex_rank"],
            vector_rank=r["vec_rank"],
        )
        for r in kept
    ], report


def best_similarity(hits: list[Hit]) -> float | None:
    """Highest cosine similarity among the hits, or None if nothing was embedded."""
    sims = [h.similarity for h in hits if h.similarity is not None]
    return max(sims) if sims else None
