"""Pure timestamp-based keyword matching for optional semantic slice selection.

This module does not perform ASR or alter the default slicing pipeline.
Transcripts must come from an explicitly supplied, timestamped source.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptSpan:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SegmentDecision:
    start: float
    end: float
    selected: bool
    reason: str


def match_segments(
    segments: list[tuple[float, float]],
    transcript: list[TranscriptSpan],
    keywords: list[str],
) -> list[SegmentDecision]:
    """Match words case-insensitively; keep existing scene boundaries intact.

    A transcript span overlaps a scene only when the intersection has
    positive duration. Empty transcripts and empty keywords are errors,
    not implicit permission to publish unfiltered clips.
    """
    terms = [term.strip().casefold() for term in keywords if term.strip()]
    if not terms:
        raise ValueError("semantic selection requires at least one keyword")
    if not transcript:
        raise ValueError("semantic selection requires timestamped transcript")
    for span in transcript:
        if (not math.isfinite(span.start) or not math.isfinite(span.end)
                or span.start < 0 or span.end <= span.start):
            raise ValueError("invalid transcript time range")
    result = []
    for start, end in segments:
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end <= start):
            raise ValueError("invalid scene time range")
        hits = sorted({
            term for span in transcript
            if span.start < end and span.end > start
            for term in terms if term in span.text.casefold()
        })
        result.append(SegmentDecision(
            start, end, bool(hits),
            "matched: " + ", ".join(hits) if hits else "no keyword match",
        ))
    return result
