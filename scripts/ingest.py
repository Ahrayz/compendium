"""Ingestion CLI: manifest -> fetch -> normalise -> chunk -> load -> embed.

    python scripts/ingest.py --dry-run --limit 3
    python scripts/ingest.py --kind talk --limit 5
    python scripts/ingest.py --embed-only

Thin on purpose: argument parsing and reporting only. Every decision lives in
`compendium.corpus`, which is importable and testable with no CLI in the way.
"""

import argparse
import asyncio
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from compendium import db, telemetry  # noqa: E402
from compendium.config import settings  # noqa: E402
from compendium.corpus import embed, load, pipeline  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The curation decision itself, as data. Every source, with its kind, licence and
# the reason it was included.
DEFAULT_MANIFESTS = [ROOT / "corpus" / "sources.json"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ingest", description=__doc__)
    p.add_argument("--manifest", type=pathlib.Path, action="append", default=None)
    p.add_argument("--limit", type=int, help="first N sources; 43 talks is a slow loop")
    p.add_argument("--kind", action="append", help="talk | book | blog | repo | docs")
    p.add_argument("--source", help="re-ingest exactly one URL")
    p.add_argument("--dry-run", action="store_true", help="fetch and chunk, write nothing")
    p.add_argument("--embed-only", action="store_true", help="skip to the embedding backfill")
    p.add_argument(
        "--owner",
        default="public",
        help="corpus to ingest into: 'public' (shared library) or a user id",
    )
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max-batches", type=int, default=None)
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    cfg = settings()
    cache_dir = ROOT / cfg.corpus_cache_dir

    if not args.embed_only:
        report = await pipeline.ingest(
            args.manifest or DEFAULT_MANIFESTS,
            cache_dir=cache_dir,
            limit=args.limit,
            only_url=args.source,
            kinds=set(args.kind) if args.kind else None,
            dry_run=args.dry_run,
            owner=args.owner,
        )
        print(report.render())
        if args.dry_run:
            print("dry run: nothing written")
            return 0

    if args.embed_only:
        try:
            written = await embed.backfill(args.batch_size, args.max_batches)
            print(f"embed:   {written} chunks embedded")
        except embed.BudgetExhausted as exc:
            print(f"embed:   stopped on budget — {exc}")
        except RuntimeError as exc:
            print(f"embed:   unavailable — {exc}")
            return 1

    # After the embed, not before: printing the backlog first reported "2334
    # awaiting embedding" on the line under "2334 chunks embedded".
    stats = await load.corpus_stats()
    print(
        f"corpus:  {stats.get('sources', 0)} sources, {stats.get('chunks', 0)} chunks, "
        f"{stats.get('tokens', 0)} tokens, {stats.get('unembedded', 0)} awaiting embedding"
    )

    spent = await telemetry.spend_today_usd()
    print(f"spend:   ${spent:.4f} of ${cfg.max_cost_usd_per_day:.2f} cap today")
    return 0


def main() -> int:
    logging.basicConfig(level=settings().log_level, format="%(message)s")
    args = parse_args()

    async def _main() -> int:
        try:
            return await run(args)
        finally:
            await db.close_pool()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
