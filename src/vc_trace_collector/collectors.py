"""Reviewed, isolated, resumable deterministic source collectors."""

from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import ClassVar, Protocol
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditLog, BudgetLedger, redact
from .extract import extract_feed
from .fetch import Fetcher, FetchTooLarge
from .models import (
    ApprovalStatus,
    AuditEvent,
    EventStatus,
    RawArtifact,
    SourceCandidate,
    SourceDecision,
    SourcePlan,
    SourceType,
    utc_now,
)
from .policy import InclusionStatus, RuleSet
from .storage import ArtifactStore, StateStore, append_jsonl, canonical_json


class ReviewRequired(RuntimeError):
    pass


class CollectorUnavailable(RuntimeError):
    pass


NETWORK_COLLECTION_METHODS = {
    "http",
    "rss_atom",
    "podcast_page_http",
    "podcast_enclosure_http",
    "yt_dlp_metadata",
    "youtube_captions",
    "yt_dlp_audio",
    "identity_evidence_http",
}


@dataclass
class CollectionContext:
    workspace: Path
    artifacts: ArtifactStore
    state: StateStore
    rules: RuleSet
    run_id: str
    fetcher: Fetcher | None = None
    audit: AuditLog | None = None
    budget: BudgetLedger | None = None
    approved_source_types: set[SourceType] | None = None
    maximum_media_seconds: float | None = None
    media_seconds_used: float = 0
    maximum_download_bytes: int | None = None
    downloaded_bytes_used: int = 0

    @property
    def remaining_media_seconds(self) -> float | None:
        if self.maximum_media_seconds is None:
            return None
        return max(0.0, self.maximum_media_seconds - self.media_seconds_used)

    @property
    def remaining_download_bytes(self) -> int | None:
        if self.maximum_download_bytes is None:
            return None
        return max(0, self.maximum_download_bytes - self.downloaded_bytes_used)


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collected: int = 0
    failed: int = 0
    skipped: int = 0
    excluded: int = 0
    review_required: int = 0
    media_seconds: float = 0
    downloaded_bytes: int = 0
    artifacts: list[RawArtifact] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)


class Collector(Protocol):
    source_types: set[SourceType]

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]: ...


class CollectorRegistry:
    def __init__(self, collectors: list[Collector]):
        self._collectors: dict[SourceType, Collector] = {}
        for collector in collectors:
            for source_type in collector.source_types:
                self._collectors[source_type] = collector

    def get(self, source_type: SourceType) -> Collector:
        try:
            return self._collectors[source_type]
        except KeyError as error:
            raise CollectorUnavailable(
                f"No collector for {source_type.value}"
            ) from error


def apply_decisions(plan: SourcePlan, decisions: list[SourceDecision]) -> SourcePlan:
    updated = plan.model_copy(deep=True)
    by_id = {candidate.candidate_id: candidate for candidate in updated.candidates}
    for decision in decisions:
        if decision.candidate_id not in by_id:
            raise KeyError(f"Unknown candidate: {decision.candidate_id}")
        candidate = by_id[decision.candidate_id]
        candidate.approval_status = decision.status
        candidate.decision_reason = decision.reason
        candidate.reviewed_by = decision.decided_by
        candidate.decision_at = decision.decided_at
        candidate.override_rule_ids = sorted(
            set(candidate.override_rule_ids + decision.override_rule_ids)
        )
        if decision.estimated_media_seconds is not None:
            candidate.estimated_media_seconds = decision.estimated_media_seconds
        if decision.material_role is not None:
            candidate.material_role = decision.material_role
        if decision.speaker_verified:
            if candidate.material_role != "spoken_by_target":
                raise ValueError(
                    "Speaker verification requires spoken_by_target material"
                )
            candidate.speaker_verified_by = decision.decided_by
        for field in ("channel", "programme", "company"):
            value = getattr(decision, field)
            if value is not None:
                setattr(candidate, field, value)
    return updated


def auto_approve(
    plan: SourcePlan,
    *,
    minimum_identity: float,
    minimum_source: float,
) -> SourcePlan:
    updated = plan.model_copy(deep=True)
    for candidate in updated.candidates:
        if candidate.approval_status == ApprovalStatus.REJECTED:
            continue
        if (
            candidate.identity_confidence.score >= minimum_identity
            and candidate.source_confidence.score >= minimum_source
        ):
            candidate.approval_status = ApprovalStatus.AUTO_APPROVED
            candidate.decision_reason = "Passed explicit automatic approval thresholds"
            candidate.reviewed_by = "automatic-confidence-policy"
            candidate.decision_at = utc_now()
        else:
            candidate.approval_status = ApprovalStatus.PENDING
    return updated


def _suffix_for_mime(mime_type: str | None, fallback: str = ".bin") -> str:
    if not mime_type:
        return fallback
    mime = mime_type.split(";", 1)[0].strip()
    if mime == "application/octet-stream":
        return fallback
    return mimetypes.guess_extension(mime) or fallback


def _remaining_bytes(context: CollectionContext) -> int | None:
    remaining = context.remaining_download_bytes
    if remaining is not None and remaining <= 0:
        raise RuntimeError("Downloaded-byte budget exhausted")
    return remaining


def _charge_download(context: CollectionContext, size_bytes: int) -> None:
    remaining = context.remaining_download_bytes
    if remaining is not None and size_bytes > remaining:
        raise RuntimeError("Downloaded-byte budget exceeded")
    context.downloaded_bytes_used += size_bytes


def _fetch(context: CollectionContext, url: str, *, maximum_bytes: int | None = None):
    if context.fetcher is None:
        raise CollectorUnavailable("HTTP collection requires a fetcher")
    remaining = _remaining_bytes(context)
    limit = remaining if maximum_bytes is None else maximum_bytes
    if remaining is not None and limit is not None:
        limit = min(remaining, limit)
    try:
        fetched = context.fetcher.fetch(url, maximum_bytes=limit)
    except (FetchTooLarge, httpx.TransportError, httpx.TimeoutException) as error:
        context.downloaded_bytes_used += int(getattr(error, "downloaded_bytes", 0))
        raise
    _charge_download(context, fetched.transferred_bytes)
    return fetched


def is_acquired_media(artifact: RawArtifact) -> bool:
    mime = (artifact.mime_type or "").casefold()
    return mime.startswith(("audio/", "video/")) or artifact.collection_method in {
        "podcast_enclosure_http",
        "yt_dlp_audio",
    }


class WebCollector:
    source_types: ClassVar[set[SourceType]] = {
        SourceType.WEB_PROFILE,
        SourceType.WEB_ARTICLE,
        SourceType.SUBSTACK,
        SourceType.MEDIUM,
    }

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        if context.fetcher is None:
            raise CollectorUnavailable("Web collector requires an HTTP fetcher")
        fetched = _fetch(context, source.url)
        mime = fetched.headers.get("content-type", "text/html")
        stored = context.artifacts.put_bytes(
            fetched.content,
            category="web",
            suffix=_suffix_for_mime(mime, ".html"),
            source_url=fetched.final_url,
            mime_type=mime,
            collection_method="http",
            original_metadata={
                "requested_url": fetched.requested_url,
                "canonical_url": fetched.canonical_url,
                "status_code": fetched.status_code,
                "headers": fetched.headers,
                "redirect_chain": fetched.redirect_chain,
                "attempts": fetched.attempts,
                "candidate_id": source.candidate_id,
            },
        )
        return [stored.record]


class FeedCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.RSS_FEED}

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        if context.fetcher is None:
            raise CollectorUnavailable("Feed collector requires an HTTP fetcher")
        fetched = _fetch(context, source.url)
        feed_artifact = context.artifacts.put_bytes(
            fetched.content,
            category="web",
            suffix=".xml",
            source_url=fetched.final_url,
            mime_type=fetched.headers.get("content-type", "application/xml"),
            collection_method="rss_atom",
            original_metadata={
                "candidate_id": source.candidate_id,
                "headers": fetched.headers,
                "entries": [
                    entry.model_dump(mode="json")
                    for entry in extract_feed(fetched.content, fetched.final_url)
                ],
            },
        )
        return [feed_artifact.record]


def _podcast_audio_urls(content: bytes, source_url: str) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    urls: set[str] = set()
    for element in soup.find_all(["audio", "source"]):
        value = element.get("src")
        if value:
            urls.add(urljoin(source_url, str(value)))

    # Podcast hosts frequently serialize enclosure URLs into JSON state rather
    # than audio elements. Decode JSON's escaped slash form before scanning.
    decoded = content.decode("utf-8", errors="replace").replace("\\u002F", "/")
    for match in re.findall(
        r"https?://[^\"'<>\s]+?\.(?:mp3|m4a|opus)(?:\?[^\"'<>\s]*)?",
        decoded,
        re.IGNORECASE,
    ):
        urls.add(match.replace("&amp;", "&"))
    return sorted(urls)[:4]


def _podcast_duration_seconds(content: bytes) -> float | None:
    text = content.decode("utf-8", errors="replace")
    total_time = re.search(
        r"(?i)total\s+time\s*:\s*-?(?:(\d+):)?(\d{1,2}):(\d{2})", text
    )
    if total_time:
        hours, minutes, seconds = total_time.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    iso = re.search(
        r"\bPT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?\b", text, re.IGNORECASE
    )
    if iso:
        hours, minutes, seconds = iso.groups()
        duration = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
        if duration > 0:
            return duration
    numeric = re.search(
        r'(?i)["\'](?:duration|duration_seconds)["\']\s*[:=]\s*["\']?(\d+(?:\.\d+)?)',
        text,
    )
    if numeric:
        duration = float(numeric.group(1))
        if duration > 0:
            return duration
    clock = re.search(r"(?<!\d)-?(?:(\d+):)?(\d{1,2}):(\d{2})(?!\d)", text)
    if clock:
        hours, minutes, seconds = clock.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return None


class PodcastCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.PODCAST}

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        if context.fetcher is None:
            raise CollectorUnavailable("Podcast collector requires an HTTP fetcher")
        looks_like_direct_audio = Path(urlsplit(source.url).path).suffix.casefold() in {
            ".mp3",
            ".m4a",
            ".opus",
            ".ogg",
            ".wav",
            ".flac",
        }
        if (
            looks_like_direct_audio
            and context.remaining_media_seconds is not None
            and source.estimated_media_seconds <= 0
        ):
            raise RuntimeError(
                "Direct podcast audio requires an approved duration estimate before GET"
            )
        if (
            not looks_like_direct_audio
            and context.remaining_media_seconds is not None
            and source.estimated_media_seconds <= 0
        ):
            try:
                head = context.fetcher.head(source.url)
            except Exception as error:
                raise RuntimeError(
                    "Cannot classify an extensionless podcast URL before GET; "
                    "approve a duration estimate"
                ) from error
            head_mime = head.headers.get("content-type", "").split(";", 1)[0]
            if head_mime.startswith(("audio/", "video/")):
                raise RuntimeError(
                    "Direct podcast media requires an approved duration estimate before GET"
                )
        fetched = _fetch(context, source.url)
        mime = fetched.headers.get("content-type", "text/html")
        soup = BeautifulSoup(fetched.content, "html.parser")
        site_name = soup.find("meta", attrs={"property": "og:site_name"})
        author = soup.find("meta", attrs={"name": "author"})
        programme = (
            str(site_name["content"]).strip()
            if site_name and site_name.get("content")
            else None
        )
        channel = (
            str(author["content"]).strip() if author and author.get("content") else None
        )
        discovered_title = soup.title.get_text(" ", strip=True) if soup.title else None
        exclusion = context.rules.evaluate(
            url=fetched.final_url,
            title=source.title or discovered_title,
            text=soup.get_text(" ", strip=True),
            channel=channel,
            programme=programme,
            company=source.company,
            stage="post_metadata",
        )
        override_applied = bool(
            exclusion.status == InclusionStatus.REVIEW_REQUIRED
            and exclusion.rule_id in source.override_rule_ids
        )
        duration = source.estimated_media_seconds or _podcast_duration_seconds(
            fetched.content
        )
        page = context.artifacts.put_bytes(
            fetched.content,
            category="podcast",
            suffix=_suffix_for_mime(mime, ".html"),
            source_url=fetched.final_url,
            mime_type=mime,
            collection_method="podcast_page_http",
            original_metadata={
                "candidate_id": source.candidate_id,
                "canonical_url": fetched.canonical_url,
                "headers": fetched.headers,
                "status_code": fetched.status_code,
                "title": discovered_title,
                "channel": channel,
                "programme": programme,
                "duration_seconds": duration,
                "collection_exclusion": exclusion.model_dump(mode="json"),
                "review_override_applied": override_applied,
            },
        ).record
        records = [page]
        if exclusion.status == InclusionStatus.EXCLUDED or (
            exclusion.status == InclusionStatus.REVIEW_REQUIRED and not override_applied
        ):
            return records
        if mime.split(";", 1)[0].strip().startswith("audio/"):
            if context.remaining_media_seconds is not None and not duration:
                raise RuntimeError("Cannot account direct podcast audio duration")
            return records

        audio_urls = _podcast_audio_urls(fetched.content, fetched.final_url)
        if audio_urls and context.remaining_media_seconds is not None:
            if not duration:
                raise RuntimeError(
                    "Cannot bound podcast duration before media download"
                )
            if duration > context.remaining_media_seconds:
                raise RuntimeError(
                    f"Media duration {duration:.1f}s exceeds remaining limit "
                    f"{context.remaining_media_seconds:.1f}s"
                )

        # One canonical enclosure is sufficient; hosts often expose several
        # encodings of the same episode.
        for audio_url in audio_urls[:1]:
            try:
                audio = _fetch(context, audio_url, maximum_bytes=512_000_000)
            except Exception as error:
                _audit(
                    context,
                    candidate=source,
                    status=EventStatus.FAILED,
                    summary="Podcast enclosure collection failed; page was retained",
                    details={"audio_url": audio_url, "error": str(error)},
                )
                continue
            audio_mime = audio.headers.get("content-type", "application/octet-stream")
            records.append(
                context.artifacts.put_bytes(
                    audio.content,
                    category="podcast",
                    suffix=_suffix_for_mime(
                        audio_mime,
                        Path(urlsplit(audio.final_url).path).suffix or ".bin",
                    ),
                    source_url=audio.final_url,
                    mime_type=audio_mime,
                    collection_method="podcast_enclosure_http",
                    original_metadata={
                        "candidate_id": source.candidate_id,
                        "episode_url": fetched.final_url,
                        "headers": audio.headers,
                        "status_code": audio.status_code,
                        "title": discovered_title,
                        "channel": channel,
                        "programme": programme,
                        "duration_seconds": duration,
                    },
                    parent_artifact_ids=[page.artifact_id],
                ).record
            )
        return records


class SuppliedFileCollector:
    source_types: ClassVar[set[SourceType]] = {
        SourceType.SUPPLIED,
        SourceType.LINKEDIN_EXPORT,
    }

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        parsed = urlsplit(source.url)
        path = (
            Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(source.url)
        )
        path = path.expanduser().resolve(strict=True)
        mime, _encoding = mimetypes.guess_type(path.name)
        stored = context.artifacts.put_bytes(
            path.read_bytes(),
            category="supplied"
            if source.source_type == SourceType.SUPPLIED
            else "social",
            suffix=path.suffix or ".bin",
            source_path=str(path),
            mime_type=mime,
            collection_method="supplied_snapshot",
            original_metadata={
                "candidate_id": source.candidate_id,
                "original_name": path.name,
            },
        )
        return [stored.record]


class YouTubeCollector:
    source_types: ClassVar[set[SourceType]] = {SourceType.YOUTUBE}

    def __init__(self, *, runner=subprocess.run):
        self.runner = runner

    def collect(
        self, source: SourceCandidate, context: CollectionContext
    ) -> list[RawArtifact]:
        hostname = (urlsplit(source.url).hostname or "").casefold()
        if not (
            hostname == "youtube.com"
            or hostname.endswith(".youtube.com")
            or hostname == "youtu.be"
        ):
            raise CollectorUnavailable("YouTube collector requires a YouTube URL")
        command = ["yt-dlp", "--dump-single-json", "--skip-download", source.url]
        try:
            completed = self.runner(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except FileNotFoundError as error:
            raise CollectorUnavailable(
                "Install the youtube extra to use yt-dlp"
            ) from error
        metadata = json.loads(completed.stdout)
        channel = (
            str(metadata.get("channel") or metadata.get("uploader") or "").strip()
            or None
        )
        channel_id = (
            str(metadata.get("channel_id") or metadata.get("uploader_id") or "").strip()
            or None
        )
        programme = str(metadata.get("series") or "").strip() or None
        discovered_title = str(metadata.get("title") or "").strip() or None
        metadata_decisions = [
            context.rules.evaluate(
                url=source.canonical_url,
                title=source.title or discovered_title,
                text=str(metadata.get("description") or ""),
                channel=value,
                programme=programme,
                company=source.company,
                stage="post_metadata",
            )
            for value in dict.fromkeys([channel, channel_id])
        ]
        exclusion = next(
            (
                decision
                for decision in metadata_decisions
                if decision.status == InclusionStatus.EXCLUDED
            ),
            next(
                (
                    decision
                    for decision in metadata_decisions
                    if decision.status == InclusionStatus.REVIEW_REQUIRED
                ),
                metadata_decisions[0],
            ),
        )
        override_applied = bool(
            exclusion.status == InclusionStatus.REVIEW_REQUIRED
            and exclusion.rule_id in source.override_rule_ids
        )
        video_id = str(metadata.get("id", ""))
        metadata_bytes = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        _charge_download(context, len(metadata_bytes))
        records = [
            context.artifacts.put_bytes(
                metadata_bytes,
                category="video",
                suffix=".metadata.json",
                source_url=source.canonical_url,
                mime_type="application/json",
                collection_method="yt_dlp_metadata",
                original_metadata={
                    "candidate_id": source.candidate_id,
                    "video_id": video_id,
                    "title": discovered_title,
                    "channel": channel,
                    "channel_id": channel_id,
                    "programme": programme,
                    "duration_seconds": float(metadata.get("duration") or 0),
                    "collection_exclusion": exclusion.model_dump(mode="json"),
                    "review_override_applied": override_applied,
                },
            ).record
        ]
        if exclusion.status == InclusionStatus.EXCLUDED or (
            exclusion.status == InclusionStatus.REVIEW_REQUIRED and not override_applied
        ):
            return records
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            transcript = YouTubeTranscriptApi().fetch(video_id)
            segments = [
                {"text": item.text, "start": item.start, "duration": item.duration}
                for item in transcript.snippets
            ]
        except (ImportError, Exception):
            segments = []
        if segments:
            caption_bytes = json.dumps(
                segments, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            _charge_download(context, len(caption_bytes))
            records.append(
                context.artifacts.put_bytes(
                    caption_bytes,
                    category="video",
                    suffix=".captions.json",
                    source_url=source.canonical_url,
                    mime_type="application/json",
                    collection_method="youtube_captions",
                    original_metadata={
                        "candidate_id": source.candidate_id,
                        "video_id": video_id,
                    },
                    parent_artifact_ids=[records[0].artifact_id],
                ).record
            )
        duration = float(metadata.get("duration") or 0)
        if context.remaining_media_seconds is not None and duration <= 0:
            raise RuntimeError("Cannot bound YouTube duration before media download")
        if (
            context.remaining_media_seconds is not None
            and duration > context.remaining_media_seconds
        ):
            raise RuntimeError(
                f"Media duration {duration:.1f}s exceeds remaining limit "
                f"{context.remaining_media_seconds:.1f}s"
            )
        with tempfile.TemporaryDirectory(prefix="vc-trace-youtube-") as directory:
            output_template = str(Path(directory) / "audio.%(ext)s")
            remaining_bytes = _remaining_bytes(context)
            download = [
                "yt-dlp",
                "--no-playlist",
                "--max-filesize",
                str(
                    min(512_000_000, remaining_bytes)
                    if remaining_bytes is not None
                    else 512_000_000
                ),
                "-f",
                "bestaudio/best",
                "-o",
                output_template,
                "--print",
                "after_move:filepath",
                source.url,
            ]
            try:
                downloaded = self.runner(
                    download,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                )
            except FileNotFoundError as error:
                raise CollectorUnavailable(
                    "Install the youtube extra to download YouTube audio"
                ) from error
            output_lines = [
                line.strip() for line in downloaded.stdout.splitlines() if line.strip()
            ]
            if not output_lines:
                raise RuntimeError("yt-dlp did not report a downloaded audio path")
            media_path = Path(output_lines[-1]).resolve()
            directory_path = Path(directory).resolve()
            if directory_path not in media_path.parents or not media_path.is_file():
                raise RuntimeError("yt-dlp reported an invalid audio path")
            mime, _encoding = mimetypes.guess_type(media_path.name)
            media_bytes = media_path.read_bytes()
            _charge_download(context, len(media_bytes))
            records.append(
                context.artifacts.put_bytes(
                    media_bytes,
                    category="video",
                    suffix=media_path.suffix or ".bin",
                    source_url=source.canonical_url,
                    mime_type=mime or "application/octet-stream",
                    collection_method="yt_dlp_audio",
                    original_metadata={
                        "candidate_id": source.candidate_id,
                        "video_id": video_id,
                        "title": discovered_title,
                        "channel": channel,
                        "channel_id": channel_id,
                        "programme": programme,
                        "duration_seconds": duration,
                    },
                    parent_artifact_ids=[records[0].artifact_id],
                ).record
            )
        return records


def default_registry() -> CollectorRegistry:
    return CollectorRegistry(
        [
            WebCollector(),
            FeedCollector(),
            PodcastCollector(),
            SuppliedFileCollector(),
            YouTubeCollector(),
        ]
    )


def _audit(
    context: CollectionContext,
    *,
    candidate: SourceCandidate,
    status: EventStatus,
    summary: str,
    outputs: list[str] | None = None,
    details: dict[str, object] | None = None,
) -> None:
    if context.audit:
        context.audit.append(
            AuditEvent(
                event_id=str(uuid4()),
                run_id=context.run_id,
                stage="collect",
                action="collect_source",
                status=status,
                summary=summary,
                input_ids=[candidate.candidate_id],
                output_ids=outputs or [],
                details=details or {},
            )
        )


def collect_approved_sources(
    plan: SourcePlan,
    *,
    context: CollectionContext,
    registry: CollectorRegistry | None = None,
) -> CollectionResult:
    pending = [
        candidate
        for candidate in plan.candidates
        if candidate.approval_status == ApprovalStatus.PENDING
    ]
    if plan.requires_review and pending:
        raise ReviewRequired(f"Source-plan review required: {plan.plan_id}")

    registry = registry or default_registry()
    result = CollectionResult()
    for candidate in plan.candidates:
        downloaded_at_start = context.downloaded_bytes_used
        if candidate.approval_status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.AUTO_APPROVED,
        }:
            continue
        if (
            context.approved_source_types is not None
            and candidate.source_type not in context.approved_source_types
        ):
            result.skipped += 1
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.SKIPPED,
                summary="Source type is outside the run allowlist",
                details={"source_type": candidate.source_type.value},
            )
            continue
        exclusion = context.rules.evaluate(
            url=candidate.canonical_url,
            title=candidate.title,
            text=candidate.description,
            channel=candidate.channel,
            programme=candidate.programme,
            company=candidate.company,
            stage="pre_collection",
        )
        if exclusion.status == InclusionStatus.EXCLUDED:
            result.excluded += 1
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.SKIPPED,
                summary="Source excluded before collection",
                details=exclusion.model_dump(mode="json"),
            )
            continue

        input_hash = sha256(
            canonical_json(candidate.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        operation_id = f"collect:{candidate.candidate_id}"
        if context.state.is_complete(operation_id, input_hash):
            result.skipped += 1
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.SKIPPED,
                summary="Previously completed collection reused",
            )
            continue

        if context.budget:
            context.budget.reserve(operation_id, candidate.estimated_cost_usd)
        context.state.start_operation(operation_id, input_hash)
        try:
            collector = registry.get(candidate.source_type)
            records = collector.collect(candidate, context)
        except Exception as error:
            result.downloaded_bytes += (
                context.downloaded_bytes_used - downloaded_at_start
            )
            if context.budget:
                context.budget.release(operation_id)
            failure = redact(
                {
                    "candidate_id": candidate.candidate_id,
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
            )
            context.state.fail_operation(
                operation_id, input_hash, str(failure["message"])
            )
            result.failed += 1
            result.failures.append(failure)
            append_jsonl(context.workspace / "audit/failures.jsonl", failure)
            _audit(
                context,
                candidate=candidate,
                status=EventStatus.FAILED,
                summary="Source collection failed without affecting other sources",
                details=failure,
            )
            continue

        output_ids = [record.artifact_id for record in records]
        media_seconds = max(
            (
                float(record.original_metadata.get("duration_seconds") or 0)
                for record in records
                if is_acquired_media(record)
            ),
            default=0.0,
        )
        context.media_seconds_used += media_seconds
        result.media_seconds += media_seconds
        result.downloaded_bytes += context.downloaded_bytes_used - downloaded_at_start
        post_collection_excluded = any(
            record.original_metadata.get("collection_exclusion", {}).get("status")
            == InclusionStatus.EXCLUDED
            for record in records
        )
        post_collection_review = any(
            record.original_metadata.get("collection_exclusion", {}).get("status")
            == InclusionStatus.REVIEW_REQUIRED
            and not record.original_metadata.get("review_override_applied", False)
            for record in records
        )
        if post_collection_excluded:
            result.excluded += 1
        if post_collection_review:
            result.review_required += 1
        if context.budget:
            context.budget.settle(operation_id, candidate.estimated_cost_usd)
        context.state.finish_operation(operation_id, input_hash, ",".join(output_ids))
        result.collected += 1
        result.artifacts.extend(records)
        _audit(
            context,
            candidate=candidate,
            status=EventStatus.SUCCEEDED,
            summary=(
                "Source metadata collected; media excluded by post-metadata rules"
                if post_collection_excluded
                else (
                    "Source metadata collected; human review required before media"
                    if post_collection_review
                    else "Source collected"
                )
            ),
            outputs=output_ids,
        )
    return result
