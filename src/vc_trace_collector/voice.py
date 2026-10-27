"""Voice-reference helpers: suggestions, speaker grouping, clips, candidate lookup."""

import math
import re
from collections import defaultdict

from .av import DiarizedTurn

MAX_REFERENCE_SECONDS = 30.0


def speakers(turns: list[DiarizedTurn]) -> list[tuple[str, float, DiarizedTurn]]:
    """(label, total seconds, longest turn), most talkative first."""
    total: dict[str, float] = defaultdict(float)
    longest: dict[str, DiarizedTurn] = {}
    for turn in turns:
        length = turn.end_seconds - turn.start_seconds
        total[turn.speaker_label] += length
        best = longest.get(turn.speaker_label)
        if best is None or length > best.end_seconds - best.start_seconds:
            longest[turn.speaker_label] = turn
    return sorted(
        ((label, total[label], longest[label]) for label in total),
        key=lambda row: -row[1],
    )


def reference_interval(turn: DiarizedTurn) -> tuple[float, float]:
    return turn.start_seconds, min(
        turn.end_seconds, turn.start_seconds + MAX_REFERENCE_SECONDS
    )


def _seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def parse_range(text: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*([\d:.]+)\s*-\s*([\d:.]+)\s*", text)
    if not match:
        raise ValueError("Use a range like 03:10-03:40")
    try:
        start, end = _seconds(match[1]), _seconds(match[2])
    except ValueError:
        raise ValueError("Use a range like 03:10-03:40") from None
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError("Start must be before end")
    return start, end
