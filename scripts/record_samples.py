"""Re-record the landing page's example answers.

    python scripts/record_samples.py

Runs each example question through the real pipeline and writes the resulting
/ask payload to src/compendium/samples/. Run it after any change to the corpus,
the retrieval path or the prompt: a recording that no longer matches what the
system would say today is worse than no recording.

Costs one real model call per example (~$0.005 each).
"""

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from compendium import db  # noqa: E402
from compendium.main import AskRequest, ask  # noqa: E402
from compendium.sample_answers import SAMPLES  # noqa: E402

EXAMPLES = [
    ("ecs-or-hierarchy", "Should I use ECS or a traditional object hierarchy?", "test1"),
    (
        "imported-3d-assets",
        "How do I optimize performance from externally imported 3D model assets?",
        "test1",
    ),
]


async def main() -> int:
    await db.open_pool()
    SAMPLES.mkdir(parents=True, exist_ok=True)
    for slug, question, user in EXAMPLES:
        response = await ask(AskRequest(question=question, user=user, limit=8))
        payload = response.model_dump()
        payload["recorded_for_user"] = user
        payload["cached"] = True
        path = SAMPLES / f"{slug}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            f"{path.name}: {len(payload['citations'])} citations, "
            f"{len(payload['passages'])} passages, ${payload['cost_usd']:.4f}, "
            f"{payload['elapsed_ms']}ms"
        )
    await db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
