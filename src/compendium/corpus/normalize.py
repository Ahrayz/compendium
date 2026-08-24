"""Pure text cleanup. No network, no database, no clock.

Normalisation is part of the `content_hash` contract. `load.py` only rewrites a
chunk when its hash changed, and `embed.py` only spends money on chunks whose
embedding is NULL. Change how whitespace is collapsed and every hash changes,
every chunk looks modified, and the whole corpus re-embeds. Treat edits here as a
deliberate backfill, never a tidy-up.
"""

import re
from typing import Any

# Caption artifacts: [Music], [Applause], [ __ ]. Bounded length so a bracketed
# aside inside real speech is not swallowed. 531 of these appeared in one
# recording, 3-13 in actual talks.
_ARTIFACT = re.compile(r"\[[^\]]{1,20}\]")
_WHITESPACE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """Normalise one cue's text.

    Deliberately does NOT lowercase or strip punctuation: auto-captions carry
    47-117 sentence marks per 1,000 words and `chunk.py` breaks on exactly those.
    Idempotent, so clean_text(clean_text(x)) == clean_text(x).
    """
    return _WHITESPACE.sub(" ", _ARTIFACT.sub(" ", text)).strip()


def clean_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clean cue text, keeping `start` and `duration`.

    Cues that are empty after stripping (a lone `[Music]`) are dropped rather
    than kept as timing anchors — `chunk.py` derives timings only from cues that
    survive, so a hole here never becomes a wrong timestamp.
    """
    out = []
    for seg in segments:
        text = clean_text(str(seg.get("text", "")))
        if not text:
            continue
        out.append(
            {
                "text": text,
                "start": float(seg.get("start", 0.0)),
                "duration": float(seg.get("duration", 0.0)),
            }
        )
    return out


def html_to_text(html: str) -> str:
    """Crude HTML-to-text for prose sources.

    Deliberately regex-based rather than a parser dependency: most text sources in
    the corpus are simple article pages. It strips script/style bodies and the
    semantic boilerplate elements, drops tags, unescapes entities and collapses
    whitespace.

    THE LIMIT OF THIS APPROACH, MEASURED. Stripping `<nav>`/`<footer>`/`<aside>`
    works only on pages that use those elements. On three real pages:

        google.github.io/draco   790 -> 672 chars   the whole menu goes
        Unreal Nanite docs    15,592 -> 15,370      barely moves
        Unity LOD manual       4,016 -> 4,016       no effect at all

    Unity and Epic build their sidebars from plain `<div>`s, so their pages still
    carry "Manual Scripting API unity.com Version: Unity 6.1 English 中文 日本語"
    into the first chunk, which dilutes it and costs real retrieval quality. That
    is the documented signal to reach for a proper content extractor, and it has
    now been met. Not done here because it is a dependency
    plus a full re-ingest, not a regex.
    """
    import html as html_mod

    html = re.sub(r"(?is)<(script|style|noscript|svg|form|select)[^>]*>.*?</\1>", " ", html)
    # Boilerplate elements, where a page bothers to mark them up as such.
    html = re.sub(r"(?is)<(nav|footer|aside)\b[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = html_mod.unescape(text)
    lines = [_WHITESPACE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# Prose has no cues, but the chunker groups cues and never splits one. So prose
# gets split into sentence-sized pseudo-cues first; otherwise a whole article
# becomes one 4,000-token chunk that matches every query and cites nothing useful.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def to_pseudo_cues(text: str, *, max_chars: int = 400) -> list[dict[str, float | str]]:
    """Split prose into cue-shaped pieces the chunker can group.

    Splits on line breaks first (html_to_text preserves paragraph boundaries),
    then on sentence boundaries for any line long enough to matter. Timestamps are
    zero here and are nulled after chunking — prose has no position in time, and
    storing 0.0 would imply it does.
    """
    pieces: list[str] = []
    for line in text.splitlines():
        line = clean_text(line)
        if not line:
            continue
        if len(line) <= max_chars:
            pieces.append(line)
            continue
        for sentence in _SENTENCE_SPLIT.split(line):
            sentence = sentence.strip()
            if sentence:
                pieces.append(sentence)
    return [{"text": p, "start": 0.0, "duration": 0.0} for p in pieces]
