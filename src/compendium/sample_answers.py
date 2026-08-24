"""Pre-recorded answers for the two example questions on the landing page.

WHY THIS EXISTS. The examples are what a first-time visitor clicks, and most
visitors click both. Every click is otherwise a real model call: cost on someone
else's demo, five to seven seconds of waiting, and a different answer each time so
nothing anyone reports can be reproduced. Serving a recording removes all three.

WHAT IT IS NOT. These are not hand-written marketing copy. Each file is the exact
`/ask` payload the live system produced, captured by `scripts/record_samples.py`,
including the passages it rejected and what it cost at the time. Re-record after
any change to the corpus or the retrieval path — a recording that no longer matches
what the system would now say is a lie with a timestamp on it.

THE DISCLOSURE RULE. A cached response carries `cached: true` and the UI says so.
This matters more than it looks: the entire claim of the product is that you can
see what actually happened. Quietly replaying a recording while implying a live run
would undermine the one thing it is trying to demonstrate. The moment the question
is edited by even one character it misses the lookup and runs for real.
"""

import json
import pathlib
import re

SAMPLES = pathlib.Path(__file__).parent / "samples"

_WS = re.compile(r"\s+")


def normalise(question: str) -> str:
    """Whitespace and case are not meaningful differences; anything else is.

    Deliberately strict beyond that. "Should I use ECS?" is a different question
    from the recorded one and must run live — a fuzzy match here would serve an
    answer to a question nobody asked, which is exactly the failure this product
    exists to argue against.
    """
    return _WS.sub(" ", question.strip().lower())


def _load() -> dict[tuple[str, str | None], dict]:
    out: dict[tuple[str, str | None], dict] = {}
    if not SAMPLES.is_dir():
        return out
    for path in sorted(SAMPLES.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        out[(normalise(payload["question"]), payload.get("recorded_for_user"))] = payload
    return out


_CACHE = _load()


def lookup(question: str, user: str | None) -> dict | None:
    """The recorded answer for this exact question in this exact corpus, if any.

    Keyed on the user too: the same question against a different corpus is a
    different question, and answering it from another corpus's recording would be
    wrong in the most confusing possible way.
    """
    return _CACHE.get((normalise(question), user))


def entries() -> list[tuple[str, str | None]]:
    """(question, user) for every recording, so the page can say which questions
    are answered from one *before* the reader clicks."""
    return [(p["question"], p.get("recorded_for_user")) for p in _CACHE.values()]
