"""Read the source manifests into `Source` rows, and enforce corpus policy.

This is the policy gate. Nothing downstream re-checks, so nothing gets past here
that should not be in the corpus.
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from compendium.schema import Source

# Corpus policy is a legal constraint (README.md). GDC Vault, published books and
# paid postmortems must not be ingested. The knowledge base contains three
# gdcvault.com links today, so "remember not to add those" is not a control.
DENY_DOMAINS = frozenset({"gdcvault.com", "vault.gdconf.com"})

_INLINE = re.compile(r'\[([^\]]*)\]\(\s*(https?://[^\s)]+?)(?:\s+"([^"]*)")?\s*\)')
_REFDEF = re.compile(r'^\[([^\]]+)\]:\s*(https?://\S+?)(?:\s+"([^"]*)")?\s*$', re.M)

_VIDEO_DOMAINS = ("youtube.com", "youtu.be")
_REPO_DOMAINS = ("github.com", "gitlab.com", "sourceforge.net")


class PolicyViolation(ValueError):
    """Raised when a source may not be ingested."""


def registrable_domain(url: str) -> str:
    """Host without a leading `www.`. Compared whole, never by substring — a
    substring test would let `notgdcvault.com` through."""
    return urlparse(url).netloc.lower().removeprefix("www.")


# Query parameters that identify a page rather than track a visit. Everything else
# is dropped: utm_*, fbclid, ref and friends make the same page look like many.
_MEANINGFUL_PARAMS = frozenset({"v", "t", "id", "p", "page", "q"})


def canonical_url(url: str) -> str:
    """One spelling per document, so the (owner, url) key can do its job.

    `http://gameprogrammingpatterns.com/contents.html` and the `https://` spelling
    were stored as two sources with byte-identical text, because the natural key
    compares strings and those strings differ. Canonicalising at parse time — before
    the key is ever used — is cheaper than detecting the duplicate afterwards.

    Deliberately conservative. Scheme, host case, `www.`, a trailing slash and
    tracking parameters never change which document you get. A path's case, or a
    parameter like `?v=`, absolutely can, so those are left alone.
    """
    # youtu.be/ID and youtube.com/watch?v=ID are the same talk. `video_id` already
    # knows how to read both, so canonicalise through it rather than duplicating
    # the URL shapes here.
    if (vid := video_id(url)) is not None:
        u = urlparse(url)
        start = parse_qs(u.query).get("t", [""])[0]
        tail = f"&t={start}" if start else ""
        return f"https://youtube.com/watch?v={vid}{tail}"

    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.")
    path = u.path.rstrip("/") or "/"
    params = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() in _MEANINGFUL_PARAMS
    ]
    query = urlencode(sorted(params))
    # Fragments address a position inside a document, not a different document.
    return urlunparse(("https", host, path, "", query, ""))


def assert_permitted(url: str) -> None:
    domain = registrable_domain(url)
    if domain in DENY_DOMAINS:
        raise PolicyViolation(f"{domain} is excluded by corpus policy: {url}")


def video_id(url: str) -> str | None:
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.")
    if host.endswith("youtu.be"):
        return (u.path.lstrip("/")[:11]) or None
    if "youtube.com" in host and u.path.startswith("/watch"):
        v = parse_qs(u.query).get("v", [None])[0]
        return v[:11] if v else None
    return None


def is_index_page(url: str) -> bool:
    """Navigation, not content: playlists, channel pages, tables of contents.

    A YouTube playlist has no `v=` parameter, so it would otherwise fall through
    to the HTML fetcher, get stored as a talk with no timed cues, and be rejected
    by `sources_talk_needs_segments_ck` at insert time. Skip it at the source.
    """
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.")
    if "youtube.com" in host or "youtu.be" in host:
        return video_id(url) is None
    return False


def _kind_for(url: str) -> str:
    domain = registrable_domain(url)
    if any(d in domain for d in _VIDEO_DOMAINS):
        return "talk"
    if any(d in domain for d in _REPO_DOMAINS):
        return "repo"
    return "blog"


def _license_for(kind: str, url: str) -> str:
    if kind == "talk":
        # Free on the official GDC channel. Indexing is permitted; reproducing
        # substantial transcript text is not — that rule lives in the answer path.
        return "gdc-youtube-free"
    if kind == "repo":
        return "public-source"
    return "public-web"


def parse_markdown(path: Path) -> list[Source]:
    """Parse a curated knowledge base.

    Handles BOTH link styles. The knowledge base uses inline links in the GDC
    sections and a reference-definition block for libraries; parsing only the
    first finds 53 of 133 URLs.

    96 of those links carry a curator-written description in the link title
    (year, speaker, studio, topic). That becomes `note` — no summary generation
    needed for them.
    """
    text = path.read_text(encoding="utf-8")
    found: dict[str, Source] = {}

    def add(label: str, url: str, desc: str | None) -> None:
        url = url.rstrip(">").rstrip(",")
        # Canonicalise before the dedup check, or the same document linked as
        # http:// in one place and https:// in another yields two sources.
        url = canonical_url(url)
        if url in found or is_index_page(url):
            return
        kind = _kind_for(url)
        found[url] = Source(
            url=url,
            kind=kind,
            title=label.strip() or url,
            license=_license_for(kind, url),
            note=(desc or "").strip() or None,
            metadata={"manifest": path.name, **({"video_id": v} if (v := video_id(url)) else {})},
        )

    for m in _INLINE.finditer(text):
        add(m.group(1), m.group(2), m.group(3))
    for m in _REFDEF.finditer(text):
        add(m.group(1), m.group(2), m.group(3))
    return list(found.values())


def parse_sources_json(path: Path) -> list[Source]:
    """Parse `corpus/sources.json`, which already carries kind, licence and note."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw["sources"] if isinstance(raw, dict) and "sources" in raw else raw
    out = []
    for item in items:
        kind = item.get("kind", "blog")
        kind = "docs" if kind == "docs" else kind
        out.append(
            Source(
                url=canonical_url(item["url"]),
                kind=kind if kind in {"talk", "book", "blog", "repo", "thread", "docs"} else "blog",
                title=item.get("title") or item["url"],
                author=item.get("author"),
                license=item.get("license") or "public-web",
                note=item.get("note"),
                metadata={"manifest": path.name},
            )
        )
    return out


def load_manifest(*paths: Path, skip_denied: bool = True) -> tuple[list[Source], list[str]]:
    """Load every manifest, de-duplicated by URL. Returns (sources, denied_urls).

    Denied URLs are returned rather than silently dropped: a run that quietly
    discards sources reads as "everything was ingested".
    """
    by_url: dict[str, Source] = {}
    denied: list[str] = []
    for path in paths:
        parsed = parse_sources_json(path) if path.suffix == ".json" else parse_markdown(path)
        for source in parsed:
            try:
                assert_permitted(source.url)
            except PolicyViolation:
                denied.append(source.url)
                if skip_denied:
                    continue
                raise
            by_url.setdefault(source.url, source)
    return list(by_url.values()), denied
