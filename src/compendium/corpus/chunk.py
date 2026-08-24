"""Pure: group timed cues into retrieval-sized chunks. No network, no database.

Reads cleaned cues (see `normalize.py`) and returns `Chunk` rows with `source_id`
unset — `load.py` supplies it. That is what lets chunking be re-run against a bare
JSON file with no database in sight, which is the whole point of the split.

Shape of the real data, measured across four GDC transcripts:
    cue length      22-38 chars median -> 2-7 words. A cue is NOT a chunk.
    400-word window ~54 cues, ~2.4 min -> a 30-min talk yields ~15 chunks
    timing overlap  95-100% of cues    -> start+duration runs past the next cue
"""

import functools
import hashlib
import re
from decimal import Decimal
from typing import Any

from compendium.schema import Chunk

_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")

TOKENIZER = "cl100k_base"


@functools.lru_cache(maxsize=1)
def _encoding():
    import tiktoken

    return tiktoken.get_encoding(TOKENIZER)


def count_tokens(text: str) -> int:
    """Exact token count for the embedding model's encoding.

    `token_count` is a stored column, so switching tokenizer later is a backfill.
    Recorded in chunk metadata for that reason.
    """
    return len(_encoding().encode(text))


def content_hash(content: str) -> str:
    """sha256 over the NORMALISED content. A change detector, not a key."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _cue_end(segments: list[dict[str, Any]], i: int) -> float:
    """End of cue `i`, clamped to the next cue's start.

    Cues overlap in time on 95-100% of this data, so `start + duration` runs past
    the following cue. Unclamped, every chunk's end bleeds into the next chunk's
    beginning and the deep links drift long.
    """
    end = segments[i]["start"] + segments[i]["duration"]
    if i + 1 < len(segments):
        end = min(end, segments[i + 1]["start"])
    return max(end, segments[i]["start"])


def chunk_segments(
    segments: list[dict[str, Any]],
    *,
    target_tokens: int = 400,
    overlap_tokens: int = 50,
    max_gap_s: float = 8.0,
) -> list[Chunk]:
    """Group cues into chunks, never splitting a cue.

    Boundary rules, in priority order:
      1. A gap longer than `max_gap_s` forces a break — silence is a topic change.
      2. Past `target_tokens`, keep going until a cue ends a sentence, up to a
         hard ceiling of 1.25x so one run-on cue cannot produce a giant chunk.
      3. Chunks overlap by roughly `overlap_tokens`, so a claim spanning a
         boundary is still retrievable whole. Consequence: concatenated chunk
         text is LONGER than the source. That is intended.
    """
    if not segments:
        return []

    ceiling = int(target_tokens * 1.25)
    counts = [count_tokens(s["text"]) for s in segments]
    n = len(segments)

    chunks: list[Chunk] = []
    start = 0
    index = 0

    while start < n:
        tokens = 0
        end = start
        while end < n:
            if end > start:
                gap = segments[end]["start"] - _cue_end(segments, end - 1)
                if gap > max_gap_s:
                    break
            tokens += counts[end]
            end += 1
            if tokens >= target_tokens:
                prev = segments[end - 1]["text"]
                if _SENTENCE_END.search(prev) or tokens >= ceiling:
                    break

        text = " ".join(segments[i]["text"] for i in range(start, end))
        chunks.append(
            Chunk(
                source_id=0,  # set by load.py
                chunk_index=index,
                content=text,
                content_hash=content_hash(text),
                token_count=sum(counts[start:end]),
                start_ts=Decimal(str(round(segments[start]["start"], 3))),
                end_ts=Decimal(str(round(_cue_end(segments, end - 1), 3))),
                metadata={"tokenizer": TOKENIZER, "cues": end - start},
            )
        )
        index += 1

        if end >= n:
            break

        # Step back far enough to carry ~overlap_tokens into the next chunk, but
        # always make forward progress or this loops forever.
        back = end
        carried = 0
        while back > start + 1 and carried < overlap_tokens:
            back -= 1
            carried += counts[back]
        start = max(back, start + 1)

    return chunks


def deep_link(url: str, start_ts: Decimal | float | None) -> str:
    """Citation link. YouTube takes whole seconds as `&t=123s`.

    Rounds DOWN so the link lands just before the claim rather than just after it
    — landing late means the viewer misses the sentence being cited.
    """
    if start_ts is None:
        return url
    seconds = int(float(start_ts))
    if "youtube.com" in url or "youtu.be" in url:
        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}t={seconds}s"
    return url
