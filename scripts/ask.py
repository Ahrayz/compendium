"""Ask the corpus a question.

python scripts/ask.py "Should I use ECS or a traditional object hierarchy?"
python scripts/ask.py --retrieval-only "..."
"""

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from compendium import db, retrieve, telemetry  # noqa: E402
from compendium.answer import ask  # noqa: E402
from compendium.config import settings  # noqa: E402


async def run(args) -> int:
    if args.retrieval_only:
        for i, h in enumerate(await retrieve.search(args.question, limit=args.limit), 1):
            print(
                f"[{i}] matched={h.matched_terms} cd={h.score:.2f} "
                f"{h.timestamp:>7}  {h.source_title}"
            )
            print(f"    {h.citation}")
        return 0

    answer = await ask(args.question, limit=args.limit)
    print(answer.render())
    print()
    if answer.abstained:
        print("(abstained — retrieval found nothing worth standing on)")
    else:
        cost = telemetry.cost_usd(settings().anthropic_model, answer.tokens_in, answer.tokens_out)
        print(
            f"tokens: {answer.tokens_in} in / {answer.tokens_out} out"
            + (f"  cost: ${cost:.5f}" if cost is not None else "  cost: unpriced")
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="ask")
    p.add_argument("question")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--retrieval-only", action="store_true")
    args = p.parse_args()

    async def _main() -> int:
        try:
            return await run(args)
        finally:
            await db.close_pool()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
