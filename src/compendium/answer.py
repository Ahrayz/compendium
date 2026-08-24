"""Grounded answers: retrieve, then synthesise with citations.

Corpus policy is enforced here, not hoped for (see README.md): the system answers in
its own words, quotes at most a short phrase, and every claim carries a deep link
with a timestamp. Abstention is a first-class output — "no sources cover this" is
a correct answer, not a failure.
"""

import logging
import re
from dataclasses import dataclass, field

from compendium import retrieve, telemetry
from compendium.config import settings

log = logging.getLogger("compendium.answer")

# Below this, the retrieved chunks are noise and the honest answer is abstention.
MIN_MATCHED_TERMS = 2
MIN_SIMILARITY = retrieve.MIN_SIMILARITY
MIN_HITS = 1

# [1], [2] ... markers the model uses to attribute a claim.
_MARKER = re.compile(r"\[(\d{1,2})\]")

SYSTEM = """You answer game development questions using ONLY the excerpts provided.

Rules, in priority order:
1. If the excerpts do not support an answer, say so plainly and stop. Refusing is
   correct behaviour, never a failure.
2. Answer in your own words. Quote at most a short phrase from any excerpt.
3. Cite every claim with the [n] marker of the excerpt it came from.
4. When sources disagree, present both positions with attribution. Do not average
   them into a single bland answer — the disagreement is the useful part.
5. Be concise. A game developer is reading this between tasks."""


@dataclass(slots=True)
class Answer:
    question: str
    text: str
    hits: list[retrieve.Hit] = field(default_factory=list)
    abstained: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    terms: retrieve.TermReport | None = None
    prompt: str = ""

    @property
    def cost_usd(self) -> float | None:
        return telemetry.cost_usd(settings().anthropic_model, self.tokens_in, self.tokens_out)

    def trace(self, *, include_prompt: bool = True) -> str:
        """Everything that produced this answer, in the order it happened.

        Printed for every query. The point is that a refusal is as explainable as
        an answer — you can see which terms were treated as evidence, which were
        discarded as corpus-wide noise, and what the model was actually shown.
        """
        lines = [
            "─" * 60,
            f"question : {self.question}",
            f"terms    : {self.terms.render() if self.terms else '(none)'}",
            f"retrieved: {len(self.hits)} hits"
            + (f", best matched={max(h.matched_terms for h in self.hits)}" if self.hits else ""),
        ]
        for i, h in enumerate(self.hits, 1):
            lines.append(
                f"  [{i}] matched={h.matched_terms} cd={h.score:.2f} "
                f"{h.timestamp:>7}  {h.source_title[:52]}"
            )
        if self.abstained:
            lines.append("decision : ABSTAIN — no model call, no tokens spent")
        else:
            if include_prompt and self.prompt:
                lines += ["prompt   :", self.prompt]
            lines += [
                "response :",
                self.text,
                f"cited    : {[n for n, _ in self.cited] or 'none'}",
                f"tokens   : {self.tokens_in} in / {self.tokens_out} out"
                + (f"  cost: ${self.cost_usd:.5f}" if self.cost_usd is not None else ""),
            ]
        lines.append("─" * 60)
        return "\n".join(lines)

    @property
    def cited(self) -> list[tuple[int, retrieve.Hit]]:
        """Only the excerpts the answer actually referenced.

        Retrieval returns 6 chunks; a good answer often uses one. Listing all six
        implies six sources support the claim, which is a citation that does not
        hold up — and citations are load-bearing here, so a source nobody cited is
        a bug, not clutter.
        """
        used = sorted({int(n) for n in _MARKER.findall(self.text)})
        return [(n, self.hits[n - 1]) for n in used if 1 <= n <= len(self.hits)]

    def render(self) -> str:
        lines = [self.text, ""]
        cited = self.cited
        if cited:
            lines.append("Sources:")
            for n, h in cited:
                # Three states, not two. NULL means "written prose, never transcribed"
                # and must not be reported as a caption — labelling a blog post
                # "auto-caption" claims a machine heard it, which is the one kind of
                # provenance error this product cannot afford.
                provenance = {"manual": "  (human-verified)", "auto": "  (auto-caption)"}.get(
                    h.transcript_kind or "", ""
                )
                lines.append(f"  [{n}] {h.source_title} @ {h.timestamp}{provenance}")
                lines.append(f"      {h.citation}")
            unused = len(self.hits) - len(cited)
            if unused:
                lines.append(f"  ({unused} retrieved excerpts were not cited)")
        return "\n".join(lines)


def build_prompt(question: str, hits: list[retrieve.Hit]) -> str:
    """Deterministic prompt assembly — no network, so it is unit-testable."""
    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(f"[{i}] {h.source_title} (at {h.timestamp})\n{h.content}")
    excerpts = "\n\n".join(blocks)
    return f"Question: {question}\n\nExcerpts:\n\n{excerpts}"


def should_abstain(hits: list[retrieve.Hit]) -> bool:
    """Abstain when retrieval found nothing worth standing on.

    Two independent ways to clear the bar, because there are two ways to be
    covered. A question can share rare vocabulary with the corpus (`ECS`, a
    version number, an API name) — that is the lexical test. Or it can be about
    something the corpus discusses in different words: "optimize performance from
    externally imported 3D model assets" shares no vocabulary with "draw call
    batching" or "mesh simplification", and would be refused on the lexical test
    alone despite being squarely in domain.

    Requiring BOTH would refuse most well-covered questions. Requiring EITHER is
    still safe because the similarity floor is measured against out-of-domain
    questions, not chosen: the nearest miss this corpus can produce — indie game
    pricing, which is about games but uncovered — tops out at 0.382 against a
    floor of 0.45. See retrieve.MIN_SIMILARITY.

    With no embeddings this reduces exactly to the lexical rule it replaced.
    """
    if len(hits) < MIN_HITS:
        return True
    lexical_ok = max((h.matched_terms for h in hits), default=0) >= MIN_MATCHED_TERMS
    best = retrieve.best_similarity(hits)
    semantic_ok = best is not None and best >= MIN_SIMILARITY
    return not (lexical_ok or semantic_ok)


async def complete(prompt: str, *, hits: int = 0) -> tuple[str, int, int]:
    """The single network edge. Returns (text, tokens_in, tokens_out).

    Everything else in this module is deterministic. Keeping the model call in one
    small function is what makes the rest unit-testable: a test replaces this and
    exercises prompt assembly, abstention and citation handling with no API key,
    no network, and no flakiness.
    """
    from anthropic import AsyncAnthropic

    cfg = settings()
    client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
    async with telemetry.record(cfg.anthropic_model, "answer") as call:
        resp = await client.messages.create(
            model=cfg.anthropic_model,
            max_tokens=700,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        call.tokens_in = resp.usage.input_tokens
        call.tokens_out = resp.usage.output_tokens
        call.meta = {"hits": hits}
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text, resp.usage.input_tokens, resp.usage.output_tokens


async def ask(question: str, *, limit: int = 6, user: str | None = None) -> Answer:
    """Answer from the shared library plus, if `user` is given, that user's own
    sources. `user=None` sees only the public corpus."""
    hits, terms = await retrieve.search_traced(question, limit=limit, user=user)

    if should_abstain(hits):
        result = Answer(
            question=question,
            text=(
                "I have no sources covering this. The corpus is conference talks and "
                "engineering writing on game development; this question falls outside it."
            ),
            hits=[],
            abstained=True,
            terms=terms,
        )
        log.info("%s", result.trace())
        return result

    prompt = build_prompt(question, hits)

    text, tokens_in, tokens_out = await complete(prompt, hits=len(hits))
    result = Answer(
        question=question,
        text=text,
        hits=hits,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        terms=terms,
        prompt=prompt,
    )
    log.info("%s", result.trace(include_prompt=settings().log_prompts))
    return result
