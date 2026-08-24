# Golden set

Thirteen cases the system must keep getting right — 8 behavioural, 5 deterministic.
Four are questions a working developer actually asked; the rest exist because
something broke and must not break again.

**This is not a list of questions with good answers.** Every case names the
*behaviour* under test, the condition that decides pass or fail, and what the
system does today — including where it still fails. A golden set that only
contains passing cases is a scoreboard, not an eval.

---

## How to read a case

| Field | Meaning |
|---|---|
| **Type** | The behaviour under test, not the topic |
| **Pass** | The condition. Written so two people grading independently agree |
| **Today** | Measured, with the date. A stale result is worse than none |

Corpus at time of measurement — pin this, or scores drift for reasons that have
nothing to do with retrieval:

```
201 sources · 3,364 passages · 1,285,773 tokens · all embedded
179 shared + 18 in the `test1` corpus + 4 flagged as near-duplicates
text-embedding-3-small @ 1536 dims · measured 24 Aug 2026
```

Cases run against `user=test1` unless stated. Refusal cases must hold in **both**
corpora — a larger library must not make the system more willing to guess.

---

## Grounded answers

### G1 — Two shipped teams disagree · *type: disagreement*

> Should I use ECS or a traditional object hierarchy for a 30-person team?

**Pass:** at least two sources taking *opposing* positions are cited, each
attributed separately. A single blended paragraph fails even if it is accurate —
averaging opposing expert experience into one confident voice is the failure this
product exists to argue against.

**Today (24 Aug):** passes on retrieval. Cites the ECS FAQ alongside *Archetypal
ECS Considered Harmful?* and *Why I Removed Components From My Game*. Best
similarity 0.575; all 8 passages found by both retrievers.

**Scored as a partial pass:** both sides are cited and attributed, but the answer
does not state that the two disagree. The citation requirement is met; the
presentation is not.

### G7 — The other long-running argument · *type: disagreement*

> Is rollback netcode better than deterministic lockstep?

**Pass:** as G1. Both models named, with the condition under which each is
chosen — not a verdict.

**Today:** passes on retrieval. Similarity 0.546, 6 distinct sources including
*Deterministic Lockstep* and the Overwatch architecture talk; all 8 passages found
by both retrievers. Held out deliberately from G1 so a fix tuned to ECS vocabulary
does not silently pass the whole category.

### G6 — The question shares no words with its answer · *type: vocabulary*

> How do I optimize performance from externally imported 3D model assets?

**Pass:** at least one cited source is about mesh optimisation, LOD, or asset
pipelines. This case exists because the question contains **none** of the words its
answer is written in — *draw calls*, *LOD*, *mesh simplification*.

**Today:** passes. Similarity 0.527; the top passage shares **zero** content words
with the question. Cites *Billions of Triangles in Minutes*, *Float Compression 4*,
and the Fatshark pipeline talk at 45:18.

**Regression meaning:** if this case fails, semantic retrieval has stopped working
— most likely a missing `OPENAI_API_KEY`, which degrades silently to lexical-only.
This is the canary for that failure.

### G5 — A two-word question · *type: definitional*

> What is ECS?

**Pass:** answers with at least one citation. Must not refuse.

**Today:** passes. Similarity 0.631, 5 distinct sources.

**History:** this case failed for weeks. The refusal rule required two rare terms
in common with the corpus, and a two-word question cannot supply them. Semantic
similarity became the second, independent route to clearing the bar, and it fixed
this without loosening the refusal — see G4 and G8, which still refuse.

---

## Refusals

Refusal cases matter more than the answered ones. A system that confidently answers
a question its corpus cannot support is worse than one that declines, and the cheap
refusal is the one worth having: it happens **before** any model call, so it costs
nothing and returns in about 100 ms.

### G4 — Adjacent to the domain, absent from the corpus · *type: refuse*

> How much should I price my one-off indie game?

**Pass:** `abstained: true`, no model call, `cost_usd == 0`.

**Today:** passes. This is the hardest refusal in the set — it is *about games*, so
it is the nearest thing to a false positive this corpus can produce. Best similarity
0.382 against a floor of 0.45. If the floor is ever raised past ~0.38 this case
starts answering, which is why it is pinned here with its number.

### G8 — Plainly outside the domain · *type: refuse*

> What is the best recipe for sourdough bread?

**Pass:** as G4.

**Today:** passes. Similarity 0.191. The floor for a floor: if this ever answers,
something is badly wrong rather than subtly wrong.

### G2 — Design and market, not engineering · *type: refuse · **currently fails***

> Should I make a roguelite strategy game in auto battler genre?

**Pass:** `abstained: true`, before the model call.

**Today: FAILS, and the failure is precise.** Retrieval does not abstain — the
lexical route clears the bar on `strategy`, `auto`, `genre` while similarity is only
0.418 — so a model call happens. The model then refuses correctly and cites nothing:
*"none of them discuss roguelites, strategy games, auto battlers…"*.

So the output is honest but the system **abstained at the wrong layer**: $0.0050 and
5 seconds to say "I don't know", where the cheap test should have caught it for $0
and 100 ms. Two defences, and only the expensive one fired.

### G3 — Production estimation · *type: refuse · **currently fails***

> Can I build my current scope of game in 6 months?

**Pass:** as G2.

**Today: FAILS the same way.** Similarity 0.433; retrieved passages are the game
loop pattern, C++ guidelines and behaviour trees. The model declines and cites
nothing. Same wrong-layer abstention, same wasted call.

**What G2 and G3 are really asking of the project.** Both were written by a
developer who wanted them answered. The corpus is engineering — architecture,
rendering, networking, tooling — and holds nothing about genre design, market fit
or production estimation. There are two honest resolutions and they should not be
mixed: **narrow the claim** so the product says what it covers, or **widen the
corpus** with design talks and postmortems. Until one is chosen, these stay in the
set as failing cases rather than being quietly deleted.

---

## Deterministic checks

No judgement, no model, cheap enough to run on every commit. A failure here is
unambiguous.

### D1 — Every citation resolves

**Pass:** every `citations[].url` returns 2xx. A claim without a resolvable source
link is a bug, not a lower-quality result. `corpus/verify_sources.py` covers the
manifest; this covers what answers actually emit.

### D2 — Timestamps land inside the passage

**Pass:** for a cited talk, the `&t=` seconds fall within that chunk's
`[start_ts, end_ts]`. Captions overlap by 95–100%, so an off-by-one in the clamp
produces links that are subtly wrong while looking completely fine — the worst
failure mode available to a citation product.

### D3 — No flagged duplicate is ever cited

**Pass:** no cited source has `duplicate_of IS NOT NULL`. Four sources are currently
flagged. Citing the same document twice under two URLs reads as two sources
agreeing, which is a correctness bug and not a cosmetic one.

### D4 — Provenance is three-valued

**Pass:** a source with `transcript_kind IS NULL` is never labelled `auto-caption`.
Written prose must not be described as machine-transcribed speech. Two thirds of the
corpus is prose, so a two-way test is wrong more often than right.

### D5 — Recorded examples still match reality

**Pass:** for each file in `src/compendium/samples/`, re-running the question live
yields the same cited *sources* and the same abstention verdict. Wording will differ
— that is the model, and asserting on it makes a test that fails for reasons
unrelated to the code.

A recording that no longer matches what the system would say today is a lie with a
timestamp on it. This is the check that stops the landing page drifting away from
the product behind it.

---

## Scoreboard, 24 Aug 2026

| | Cases | Pass | Fail |
|---|---|---|---|
| Disagreement | G1, G7 | 2 (retrieval only) | — |
| Vocabulary | G6 | 1 | — |
| Definitional | G5 | 1 | — |
| Refusal | G4, G8, G2, G3 | 2 | **2** |
| Deterministic | D1–D5 | run by hand | — |

**8 behavioural cases: 6 pass, 2 fail.** Both failures are the same defect —
refusal deciding too late — and both are refusals, which is the category where being
wrong costs the most trust.
