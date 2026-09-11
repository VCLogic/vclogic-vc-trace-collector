"""Run configuration loading, freezing, and deterministic hashing."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .storage import canonical_json


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    firm: str | None = None
    known_profile_url: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    output_dir: str = "outputs"
    approved_source_types: list[str] = Field(default_factory=list)
    excluded_domains: list[str] = Field(default_factory=list)
    excluded_channels: list[str] = Field(default_factory=list)
    discovery_model: str | None = None
    transcription_model: str | None = None
    diarization_model: str | None = None
    embedding_model: str | None = None
    maximum_cost_usd: Decimal = Decimal("10.00")
    maximum_search_operations: int = Field(default=20, ge=0)
    maximum_media_minutes: float = Field(default=120, ge=0)
    automatic_discovery: bool = False
    resume: str | None = None

    @property
    def fingerprint(self) -> str:
        return sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


def load_toml(path: Path) -> dict:
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)
