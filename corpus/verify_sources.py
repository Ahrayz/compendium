"""Verify every URL in sources.json actually resolves.

Citations are load-bearing — see "What may be ingested" in README.md. A claim
without a resolvable source link is a bug, not a degraded result. That standard
starts here: a curated list is worthless if the links rot, and worse than
worthless if they were never right in the first place.

    python corpus/verify_sources.py           # verify, print a report
    python corpus/verify_sources.py --json    # machine-readable, for CI

Exit code is non-zero if any source fails, so this can be wired into CI later.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

MANIFEST = Path(__file__).parent / "sources.json"
CONCURRENCY = 8
TIMEOUT = 20.0

# A 200 is not proof the content is real. Valve's wiki returns 200 with a 4KB
# proof-of-work bot wall; Cloudflare and friends do the same. Anything matching
# these is reachable by a human but not ingestable by us, which is a different
# problem from a dead link and has to be reported differently.
# Match on fragments that survive HTML entity escaping. "making sure you're not
# a bot" does not match Valve's page, because the apostrophe ships as &#39;.
CHALLENGE_MARKERS = (
    "not a bot",
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "checking your browser",
    "enable javascript and cookies",
)

# Below this, a 200 is almost always a stub, a placeholder or a challenge page
# rather than an article.
MIN_BYTES = 6_000

# Some hosts reject HEAD or bot-ish clients. A real browser UA avoids false
# failures that would otherwise get a perfectly good source deleted.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class Result:
    url: str
    title: str
    ok: bool
    status: int | None
    detail: str
    warning: str = ""

    @property
    def final_note(self) -> str:
        return self.detail or (str(self.status) if self.status else "")


async def check(client: httpx.AsyncClient, source: dict, sem: asyncio.Semaphore) -> Result:
    """GET (not HEAD) so the body can be sanity-checked, not just the status."""
    url = source["url"]
    title = source.get("title", "")
    async with sem:
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            return Result(url, title, False, None, type(exc).__name__)

    detail = f"-> {response.url}" if str(response.url) != url else ""
    if response.status_code >= 400:
        return Result(url, title, False, response.status_code, detail)

    body = response.text
    lowered = body[:20_000].lower()

    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return Result(url, title, True, response.status_code, detail, "bot wall")

    thin_exempt = source.get("kind") == "repo" or source.get("thin_ok")
    if len(body) < MIN_BYTES and not thin_exempt:
        return Result(url, title, True, response.status_code, detail, f"thin ({len(body)}B)")

    return Result(url, title, True, response.status_code, detail)


async def run(sources: list[dict]) -> list[Result]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT, headers=HEADERS) as client:
        return await asyncio.gather(*(check(client, s, sem) for s in sources))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sources = manifest["sources"]
    results = asyncio.run(run(sources))

    passed = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    warned = [r for r in results if r.ok and r.warning]

    if args.json:
        print(
            json.dumps(
                {
                    "total": len(results),
                    "passed": len(passed),
                    "failed": [
                        {"url": r.url, "status": r.status, "detail": r.detail} for r in failed
                    ],
                    "warnings": [{"url": r.url, "warning": r.warning} for r in warned],
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    by_kind: dict[str, int] = {}
    for source, result in zip(sources, results, strict=True):
        if result.ok:
            by_kind[source["kind"]] = by_kind.get(source["kind"], 0) + 1

    print(f"\n  {len(passed)}/{len(results)} sources verified\n")
    for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"    {kind:<6} {count}")

    if failed:
        print(f"\n  {len(failed)} FAILED\n")
        for r in failed:
            print(f"    [{r.final_note or 'error':<12}] {r.title}")
            print(f"                   {r.url}")

    if warned:
        print(f"\n  {len(warned)} reachable but NOT ingestable\n")
        for r in warned:
            print(f"    [{r.warning:<12}] {r.title}")
            print(f"                   {r.url}")

    redirected = [r for r in results if r.ok and r.detail]
    if redirected:
        print(f"\n  {len(redirected)} redirected (consider updating the manifest)\n")
        for r in redirected:
            print(f"    {r.url}")
            print(f"      {r.detail}")

    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
