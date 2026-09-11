"""Run configuration loading, freezing, and deterministic hashing."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import MaterialRole
from .policy import ExclusionRule
from .storage import canonical_json


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    firm: str | None = None
    known_profile_url: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    supplied_files: list[str] = Field(default_factory=list)
    supplied_role: MaterialRole = MaterialRole.UNKNOWN
    output_dir: str = "outputs"
    approved_source_types: list[str] = Field(default_factory=list)
    excluded_domains: list[str] = Field(default_factory=list)
    excluded_channels: list[str] = Field(default_factory=list)
    exclusion_rules: list[ExclusionRule] = Field(default_factory=list)
    discovery_model: str | None = None
    transcription_model: str | None = None
    diarization_model: str | None = None
    embedding_model: str | None = None
    transcription_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    diarization_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    embedding_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    search_operation_cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    speaker_minimum_score: float = Field(default=0.75, ge=-1, le=1)
    speaker_minimum_margin: float = Field(default=0.10, ge=0, le=2)
    maximum_cost_usd: Decimal = Decimal("10.00")
    discovery_call_budget_usd: Decimal | None = Field(default=None, ge=0)
    maximum_search_operations: int = Field(default=20, ge=0)
    maximum_media_minutes: float = Field(default=120, ge=0)
    maximum_download_bytes: int = Field(default=1_000_000_000, gt=0)
    maximum_provider_operations: int = Field(default=100, ge=0)
    automatic_discovery: bool = False
    allow_partial_run: bool = False
    resume: str | None = None

    @property
    def fingerprint(self) -> str:
        return sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


def load_toml(path: Path) -> dict:
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)
