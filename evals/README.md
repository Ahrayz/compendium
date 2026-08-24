# Evals

[`golden.md`](golden.md) is the set: thirteen cases, each naming a behaviour, a
pass condition, and what the system does today.

## Why these are evals and not tests

The unit tests in [`tests/`](../tests) assert on deterministic code — query
construction, the refusal rule, prompt assembly, citation extraction. They must
pass on every commit, and they never call a model.

Evals judge behaviour that is probabilistic. They cost money, take seconds, and can
legitimately move without anyone changing code — a model updates, a source is
re-chunked. Gating every commit on them would make the build flaky for reasons that
are not defects.

The rule this project uses: **if a case can be decided without a model, it belongs
in `tests/`.** Everything left is an eval.

## What a case has to state

Four things, or the score is not reproducible six weeks later:

| Part | Here |
|---|---|
| **Request** | The question, as a developer would actually type it |
| **Environment** | Which sources were in the corpus, pinned. Scores drift with the library, not just with the code |
| **Stopping criteria** | What counts as finished — a cited answer, or a refusal |
| **Scorer** | How this case is graded, and by what |

## Three kinds of grader

In increasing order of how much they should be trusted:

1. **Deterministic.** Does every cited link resolve? Does the timestamp land inside
   the passage it came from? Was a flagged duplicate cited? Cheap, exact, no
   judgement — and the only kind currently worth putting in CI. Cases D1–D5.
2. **Code-based.** Abstention rate on the refusal cases, recall of an expected
   source, distinct sources per answer. Still no judgement, but it needs a label.
3. **LLM-as-judge.** Answer quality only, and only where the first two cannot
   reach. Judges are biased toward longer and more confident answers, which is
   exactly the bias this product is built against — so check a judge against your
   own labels before believing it.

## Grade behaviour, never wording

No case asserts on the model's phrasing. Model output is probabilistic; asserting on
it produces a failure that says nothing about the code. Every pass condition here is
about *which sources were cited*, *whether it refused*, and *what it cost* — all of
which are stable when the wording is not.

## Failures earn their place

A case gets added when something breaks, and it stays after the fix. Two cases in
the set fail today and are documented as failing rather than removed. A golden set
that only contains passing cases has stopped being an instrument and become a
scoreboard.

## How it is run

By hand, against a pinned corpus, with the result and date written back into
`golden.md`. The deterministic checks are the ones worth automating first — they
need no labels and no judgement — and an LLM judge is the last thing to add rather
than the first.
