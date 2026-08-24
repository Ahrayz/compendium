"""Orchestration: manifest -> fetch -> normalise -> chunk -> load.

Kept separate from the CLI so it is importable and testable. The CLI is argument
parsing and printing, nothing else.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from compendium.corpus import chunk as chunker
from compendium.corpus import dedupe, fetch, load, manifest, normalize
from compendium.schema import Source

log = logging.getLogger("compendium.ingest")


@dataclass
class Report:
    """What happened. Printed in full every run, including the empty lists —
    silence about what was dropped reads as 'everything worked'."""

    ingested: int = 0
    unchanged: int = 0
    chunks_written: int = 0
    chunks_deleted: int = 0
    denied: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    duplicates: list[tuple[str, str, float]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"sources: {self.ingested} ingested, "
            f"{len(self.skipped)} skipped, {len(self.denied)} denied by policy",
            f"chunks:  {self.chunks_written} written, {self.chunks_deleted} deleted",
        ]
        for url in self.denied:
            lines.append(f"  denied   {url}")
        for url, reason in self.skipped:
            lines.append(f"  skipped  {reason:<22} {url}")
        for url, of, score in self.duplicates:
            lines.append(f"  dup {score:.2f}  {url}\n             already covered by: {of}")
        return "\n".join(lines)


def chunks_for(source: Source) -> list:
    """Normalise then chunk, choosing the strategy by what the source carries."""
    if source.raw_segments:
        return chunker.chunk_segments(normalize.clean_segments(source.raw_segments))
    if source.raw_text:
        cues = normalize.to_pseudo_cues(source.raw_text)
        chunks = chunker.chunk_segments(cues)
        # Prose has no position in time. Storing 0.0 would claim it does, and a
        # citation would deep-link to the start of a document that has no start.
        for c in chunks:
            c.start_ts = None
            c.end_ts = None
        return chunks
    return []


async def ingest(
    manifests: list[Path],
    *,
    cache_dir: Path,
    limit: int | None = None,
    only_url: str | None = None,
    kinds: set[str] | None = None,
    dry_run: bool = False,
    owner: str = "public",
) -> Report:
    """Ingest a manifest into one corpus.

    `owner="public"` builds the shared library. Any other value builds that user's
    private corpus: the same URL can exist under several owners, each re-ingestable
    without touching the others (migrations/0003_multi_tenant.sql).
    """
    sources, denied = manifest.load_manifest(*manifests)
    report = Report(denied=denied)

    if only_url:
        sources = [s for s in sources if s.url == only_url]
    if kinds:
        sources = [s for s in sources if s.kind in kinds]
    if limit:
        sources = sources[:limit]

    for source in sources:
        result = fetch.fetch_source(source, cache_dir)
        if isinstance(result, fetch.FetchFailure):
            report.skipped.append((result.url, result.reason))
            continue

        # A talk with no timed cues cannot produce a timestamped citation, and the
        # sources_talk_needs_segments_ck constraint would reject it at insert.
        # Catch it here so the run reports a skip instead of raising.
        if result.kind == "talk" and not result.raw_segments:
            report.skipped.append((result.url, "talk without timed cues"))
            continue

        chunks = chunks_for(result)
        if not chunks:
            report.skipped.append((result.url, "no content"))
            continue

        if dry_run:
            report.ingested += 1
            report.chunks_written += len(chunks)
            log.info("[dry-run] %s -> %d chunks", result.title[:60], len(chunks))
            continue

        result.owner = owner
        if owner != "public":
            result.added_by = owner
        # Fingerprint what will actually be indexed, not the raw download: the
        # chunks are what retrieval sees, and normalisation has already run on them.
        result.shingle_sketch, result.shingle_count = dedupe.sketch(
            " ".join(c.content for c in chunks)
        )
        source_id = await load.upsert_source(result)
        written, deleted = await load.replace_chunks(source_id, chunks)

        # Checked after the write so the comparison uses the stored sketch, and so a
        # source that later stops being a duplicate can be un-flagged on re-ingest.
        dup = await load.find_duplicate(source_id, owner)
        await load.mark_duplicate(source_id, dup[0] if dup else None)
        if dup:
            report.duplicates.append((result.url, dup[1], dup[2]))
        report.ingested += 1
        report.chunks_written += written
        report.chunks_deleted += deleted
        if written == 0:
            report.unchanged += 1
        log.info("%s -> %d chunks (%d changed)", result.title[:60], len(chunks), written)

    return report
