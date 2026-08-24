"""Network I/O. The only module allowed to be slow, and the only one that caches."""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from compendium.corpus import manifest, normalize
from compendium.schema import Source

USER_AGENT = "compendium-ingest/0.1 (+https://github.com/Ahrayz/gamedev-compendium)"
TIMEOUT = 30.0


@dataclass(slots=True)
class FetchFailure:
    """A source that could not be fetched. Carried, not raised."""

    url: str
    reason: str


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.json"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _fetch_transcript(vid: str) -> tuple[list[dict], str, bool]:
    """Raw cues plus language and whether they are machine-generated."""
    from youtube_transcript_api import YouTubeTranscriptApi

    fetched = YouTubeTranscriptApi().fetch(vid)
    cues = [{"text": s.text, "start": s.start, "duration": s.duration} for s in fetched.snippets]
    return cues, fetched.language_code, bool(fetched.is_generated)


def fetch_source(source: Source, cache_dir: Path) -> Source | FetchFailure:
    """Populate `raw_segments` / `raw_text` / `transcript_kind` / `fetched_at`.

    ONE SOURCE MUST NEVER KILL THE RUN. 1 of 44 videos returns TranscriptsDisabled;
    raised out of a loop over 43 talks that means nothing gets ingested. Failures
    come back as `FetchFailure` so the CLI can report what was skipped and why.

    The cache stores the RAW response, never the normalised form — normalisation
    is a decision that will be revised, and the cache has to survive that.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(cache_dir, source.url)
    if cached.exists():
        blob = json.loads(cached.read_text(encoding="utf-8"))
    else:
        try:
            blob = _download(source)
        except Exception as exc:  # noqa: BLE001 - any failure is data, not a crash
            return FetchFailure(source.url, type(exc).__name__)
        cached.write_text(json.dumps(blob), encoding="utf-8")

    if blob.get("error"):
        return FetchFailure(source.url, blob["error"])

    if blob["type"] == "text" and (wall := challenge_marker(blob["text"])):
        # Corpus rule: "a bot challenge is a refusal". The wall returns
        # HTTP 200, so status code alone cannot see it — two Valve wiki pages were
        # ingested as 308 tokens of "Making sure you're not a bot!" and sat in the
        # corpus looking like real networking sources. Checked here, after the
        # cache, so already-cached poison is caught without a re-fetch.
        return FetchFailure(source.url, f"bot challenge ({wall})")

    source.fetched_at = _now()
    if blob["type"] == "transcript":
        source.raw_segments = blob["segments"]
        source.transcript_kind = "auto" if blob["auto"] else "manual"
        source.metadata = {**source.metadata, "language": blob["language"]}
    else:
        source.raw_text = blob["text"]
    return source


# Substrings that only appear on an interstitial, never in an article about game
# development. Matched against the first few hundred characters: an article may
# discuss CAPTCHAs, but it does not open with one.
_CHALLENGE_MARKERS = (
    "making sure you're not a bot",
    "you are seeing this because the administrator of this website",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "verify you are human",
    "just a moment...",
    "ddos protection by",
    "please complete the security check",
)

# Long enough to clear a title and a nav bar, short enough that an article merely
# mentioning these phrases later is not caught.
_CHALLENGE_WINDOW = 600


def challenge_marker(text: str) -> str | None:
    """The bot-wall phrase found at the top of the page, if any.

    Returns the marker rather than a bool so the skip line in the ingest report
    names what was seen — "bot challenge" alone gives a curator nothing to act on.
    """
    head = text[:_CHALLENGE_WINDOW].lower()
    return next((m for m in _CHALLENGE_MARKERS if m in head), None)


_GITHUB_BRANCHES = ("HEAD", "main", "master")

# GitHub is case-sensitive on raw.githubusercontent.com, and projects are not
# consistent. Tencent/rapidjson ships `readme.md`; most ship `README.md`.
_README_NAMES = ("README.md", "readme.md", "README.rst", "README")


def is_github_page(url: str) -> bool:
    return urlparse(url).netloc.lower().removeprefix("www.") == "github.com"


def github_raw_urls(url: str) -> list[str]:
    """Raw-content URLs for a GitHub page, best guess first; empty if not GitHub.

    A GitHub HTML page is mostly GitHub's own chrome — nav, sign-in, Copilot
    marketing — and `html_to_text` keeps all of it. Twenty-one sources in this
    corpus were stored as ~90% identical boilerplate, which the near-duplicate
    detector then flagged as copies of one another. They were: the pages really are
    nearly identical, because almost none of the stored text came from the project.

    Two shapes are handled:
      github.com/owner/repo                     -> the README
      github.com/owner/repo/blob/<ref>/<path>   -> that exact file

    `HEAD` is tried first for the repo case because it follows whatever the default
    branch is called, which `main`/`master` guessing does not.
    """
    parts = urlparse(url)
    if not is_github_page(url):
        return []
    seg = [s for s in parts.path.split("/") if s]

    if len(seg) >= 4 and seg[2] == "blob":
        owner, repo, _, ref, *rest = seg
        if rest:
            return [f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{'/'.join(rest)}"]
        return []

    if len(seg) == 2:
        owner, repo = seg
        return [
            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{name}"
            for branch in _GITHUB_BRANCHES
            for name in _README_NAMES
        ]
    return []


def _download(source: Source) -> dict:
    if is_github_page(source.url):
        for raw_url in github_raw_urls(source.url):
            resp = httpx.get(
                raw_url, timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            )
            if resp.status_code == 200 and resp.text.strip():
                return {"type": "text", "text": normalize.html_to_text(resp.text)}
        # Falling back to the HTML page is not an option: it is GitHub's nav and
        # marketing, not the project. Nine repos here keep their README somewhere
        # other than the root (imgui uses docs/README.md; the id-Software releases
        # have none at all), and storing chrome for them is worse than storing
        # nothing — it is retrievable, citable and meaningless. Report the gap so a
        # curator can point the manifest at the real file.
        return {"type": "error", "error": "github page without a fetchable README"}

    vid = manifest.video_id(source.url)
    if vid:
        try:
            cues, language, auto = _fetch_transcript(vid)
        except Exception as exc:  # noqa: BLE001
            # Cached as an error so a known-bad source is not retried every run.
            return {"type": "error", "error": type(exc).__name__}
        return {"type": "transcript", "segments": cues, "language": language, "auto": auto}

    resp = httpx.get(
        source.url,
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
    if resp.status_code != 200:
        # 403/406 are usually bot-walls, not link rot. Recorded, not deleted —
        # a human decides whether the source is really gone.
        return {"type": "error", "error": f"http {resp.status_code}"}
    return {"type": "text", "text": normalize.html_to_text(resp.text)}
