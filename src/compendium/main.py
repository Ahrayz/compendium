import logging
import pathlib
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from compendium import __version__, answer, db, sample_answers, telemetry
from compendium.config import settings
from compendium.corpus import load

STATIC = pathlib.Path(__file__).parent / "static"

logging.basicConfig(level=settings().log_level)
log = logging.getLogger("compendium")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    log.info("compendium %s started (env=%s)", __version__, settings().env)
    yield
    await db.close_pool()


app = FastAPI(
    title="Compendium",
    version=__version__,
    description=(
        "Ask a game dev question, get an answer grounded in real sources — every claim deep-linked."
    ),
    lifespan=lifespan,
)


async def healthz() -> dict[str, str]:
    """Liveness. Must not touch the database — a slow DB shouldn't make a
    container that is actually fine look dead."""
    return {"status": "ok", "version": __version__}


# Served on every configured alias rather than one hardcoded path: `/healthz` is
# swallowed by Google's frontend on *.run.app and never reaches us. See
# Settings.health_paths.
for _path in settings().health_path_list:
    app.add_api_route(_path, healthz, methods=["GET"], tags=["health"])


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness. Touches the database, because a instance that can't reach
    Postgres can't serve anything useful."""
    ok = await db.healthy()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "database": ok},
    )


@app.get("/metrics/cost")
async def cost() -> dict[str, float]:
    """Cost-per-query must be answerable at any time, not reconstructed later."""
    return {
        "spend_today_usd": await telemetry.spend_today_usd(),
        "budget_usd": settings().max_cost_usd_per_day,
        **await telemetry.latency_percentiles(),
    }


# Pydantic earns its place here and not in schema.py: this is the untrusted
# boundary. Rows from Postgres are already validated by the constraints in 0002;
# a request body is validated by nobody until now.
class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    limit: int = Field(default=6, ge=1, le=20)
    explain: bool = Field(default=False, description="include the retrieval trace")
    user: str | None = Field(
        default=None,
        max_length=64,
        description="search this user's own sources in addition to the shared library",
    )


class TermInfo(BaseModel):
    """One query word, and whether it counted as evidence.

    `doc_freq` is how many chunks contain it. The UI turns this into plain
    language — a word in 42% of the library cannot tell you which passage matters.
    """

    term: str
    doc_freq: int
    kept: bool


class Passage(BaseModel):
    """A retrieved chunk, and whether the answer actually leaned on it."""

    marker: int
    title: str
    url: str
    timestamp: str
    matched_terms: int
    score: float
    used: bool
    transcript_kind: str | None
    # How this passage was found. "words" = full-text match, "meaning" = vector
    # similarity, "both" = both retrievers agreed, which is the strongest signal.
    found_by: str
    similarity: float | None


class Citation(BaseModel):
    marker: int
    title: str
    url: str
    timestamp: str
    transcript_kind: str | None


class AskResponse(BaseModel):
    question: str
    answer: str
    abstained: bool
    citations: list[Citation]
    terms: list[TermInfo] = []
    passages: list[Passage] = []
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    elapsed_ms: int = 0
    trace: str | None = None
    # True when this is a recording of an earlier real run rather than a live one.
    # Always surfaced to the caller — see sample_answers for why that is not
    # optional here.
    cached: bool = False


class SampleQuestion(BaseModel):
    question: str
    user: str | None


class CorpusStats(BaseModel):
    sources: int
    chunks: int
    tokens: int
    unembedded: int
    # Which questions are answered from a recording. The page needs this to tell
    # the reader before they ask, not only after.
    samples: list[SampleQuestion] = []


@app.post("/ask")
async def ask(request: AskRequest) -> AskResponse:
    """Answer a question from the corpus, with citations.

    `abstained: true` is a successful response, not an error — the corpus does not
    cover everything and saying so is the correct behaviour. It returns 200 with an
    empty citation list, because nothing failed.
    """
    # The two landing-page examples are served from a recording of a real run.
    # Exact match only: one edited character misses this and runs live. The reply
    # says `cached: true` either way, because a system whose whole claim is "you
    # can see what happened" cannot quietly replay a recording as if it were live.
    if (recorded := sample_answers.lookup(request.question, request.user)) is not None:
        log.info("cached sample answer: %s", request.question)
        return AskResponse(**recorded)

    started = time.perf_counter()
    result = await answer.ask(request.question, limit=request.limit, user=request.user)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    cited_markers = {n for n, _ in result.cited}
    terms: list[TermInfo] = []
    if result.terms:
        terms = [TermInfo(term=t, doc_freq=0, kept=True) for t in result.terms.kept] + [
            TermInfo(term=t, doc_freq=df, kept=False) for t, df in result.terms.dropped
        ]

    return AskResponse(
        question=result.question,
        answer=result.text,
        abstained=result.abstained,
        citations=[
            Citation(
                marker=n,
                title=h.source_title,
                url=h.citation,
                timestamp=h.timestamp,
                transcript_kind=h.transcript_kind,
            )
            for n, h in result.cited
        ],
        terms=terms,
        passages=[
            Passage(
                marker=i,
                title=h.source_title,
                url=h.citation,
                timestamp=h.timestamp,
                matched_terms=h.matched_terms,
                score=round(h.score, 4),
                used=i in cited_markers,
                transcript_kind=h.transcript_kind,
                found_by=(
                    "both"
                    if h.lexical_rank and h.vector_rank
                    else ("words" if h.lexical_rank else "meaning")
                ),
                similarity=round(h.similarity, 3) if h.similarity is not None else None,
            )
            for i, h in enumerate(result.hits, 1)
        ],
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
        elapsed_ms=elapsed_ms,
        trace=result.trace(include_prompt=settings().log_prompts) if request.explain else None,
    )


@app.get("/corpus")
async def corpus() -> CorpusStats:
    """What the library actually contains. The page shows this so a viewer knows
    the answer came from a bounded, named set of sources rather than the internet."""
    stats = await load.corpus_stats()
    return CorpusStats(
        sources=int(stats.get("sources", 0)),
        chunks=int(stats.get("chunks", 0)),
        tokens=int(stats.get("tokens", 0)),
        unembedded=int(stats.get("unembedded", 0)),
        samples=[SampleQuestion(question=q, user=u) for q, u in sample_answers.entries()],
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
