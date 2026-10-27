"""Read-only dashboard projections and human reference selection helpers."""

import math
import re
from collections import Counter

from .storage import read_json, read_jsonl
from .wizard_models import source_items

APPROVED = {"approved", "auto_approved"}
UNFINISHED = {"not processed", "failed", "not_collected"}


def parse_timestamp(value: str) -> float:
    pieces = value.strip().split(":")
    if not 1 <= len(pieces) <= 3:
        raise ValueError("Use seconds, mm:ss, or hh:mm:ss")
    numbers = [float(p) for p in pieces]
    if any(not math.isfinite(n) or n < 0 for n in numbers):
        raise ValueError("Time must be finite and nonnegative")
    if len(numbers) > 1 and (
        any(n >= 60 for n in numbers[1:]) or any(n != int(n) for n in numbers[:-1])
    ):
        raise ValueError("Minutes/seconds must be below 60; fractions only in seconds")
    return sum(n * 60**i for i, n in enumerate(reversed(numbers)))


def reference_suggestions(items, name: str, limit: int = 3):
    eligible = [
        i
        for i in items
        if i.downloaded
        and i.media
        and i.candidate.approval_status in APPROVED
        and i.candidate.material_role != "third_party"
    ]

    def rank(item):
        c = item.candidate
        named = bool(
            re.search(r"\b" + re.escape(name) + r"\b", c.title or "", re.IGNORECASE)
        )
        return (
            -int(named),
            -c.identity_confidence.score,
            -int(c.material_role in {"spoken_by_target", "reference_voice"}),
            c.candidate_id,
        )

    return sorted(eligible, key=rank)[:limit]


def summarize(pipeline, slug: str) -> dict:
    from .inventory import ready_recording

    workspace = pipeline.workspace(slug)
    identity = read_json(workspace / "identity/resolved_identity.json")
    items = source_items(pipeline, slug)
    profiles = read_jsonl(workspace / "identity/reference_voice_profiles.jsonl")
    legacy = workspace / "identity/reference_voice_profile.json"
    if legacy.exists():
        profiles.append(read_json(legacy))
    reference = any(p.get("status") == "verified_human" for p in profiles)
    pending = sum(i.candidate.approval_status == "pending" for i in items)
    missing = sum(
        i.candidate.approval_status in APPROVED
        and not i.downloaded
        and i.collection_status != "excluded"
        for i in items
    )
    unprocessed = [
        i
        for i in items
        if i.downloaded
        and i.candidate.approval_status in APPROVED
        and i.candidate.material_role != "reference_voice"
        and i.processing_status in UNFINISHED
    ]
    if identity.get("resolution_status") != "confirmed":
        next_action = "identity"
    elif pending:
        next_action = "review"
    elif missing:
        next_action = "download"
    elif unprocessed:
        next_action = (
            "reference"
            if any(i.media for i in unprocessed) and not reference
            else "process"
        )
    else:
        next_action = "results"
    return {
        "slug": slug,
        "name": identity.get("canonical_name") or slug,
        "firm": ", ".join(
            dict.fromkeys(
                a["firm"] for a in identity.get("affiliations", []) if a.get("firm")
            )
        ),
        "discovered": len(items),
        "approved": sum(i.candidate.approval_status in APPROVED for i in items),
        "downloaded": sum(i.downloaded for i in items),
        "processed": sum(i.processing_status not in UNFINISHED for i in items),
        "review_required": sum(i.processing_status == "review_required" for i in items),
        "failed": sum(
            i.collection_status == "failed" or i.processing_status == "failed"
            for i in items
        ),
        "download_failed": sum(
            i.collection_status == "failed" and not i.downloaded for i in items
        ),
        "processing_failed": sum(i.processing_status == "failed" for i in items),
        "ready_to_process": sum(ready_recording(i) for i in items),
        "pending": pending,
        "missing": missing,
        "reference": reference,
        "next": next_action,
        "platforms": dict(Counter(i.candidate.source_type.value for i in items)),
    }


def workspaces(pipeline):
    rows = []
    for path in (
        sorted(pipeline.output_dir.iterdir()) if pipeline.output_dir.exists() else []
    ):
        if not path.is_dir() or not any(
            (path / name).exists()
            for name in ("identity", "discovery", "config_snapshot.json")
        ):
            continue
        slug = path.name
        try:
            rows.append(summarize(pipeline, slug))
        except Exception as error:
            rows.append(
                {
                    "slug": slug,
                    "name": slug,
                    "error": type(error).__name__,
                    "next": "repair",
                }
            )
    return rows
