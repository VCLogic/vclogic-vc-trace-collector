"""Read-only checks for local collection and Agent Reach capabilities."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Capability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "missing", "unavailable", "error"]
    required: bool = False
    executable: str | None = None
    active_backend: str | None = None
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DoctorReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    tools: dict[str, Capability] = Field(default_factory=dict)
    backends: dict[str, Capability] = Field(default_factory=dict)


_SENSITIVE_PARTS = ("token", "cookie", "password", "secret", "authorization")


def _sanitize(value: Any, *, key: str = "") -> Any:
    if any(part in key.casefold() for part in _SENSITIVE_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


class CapabilityDoctor:
    """Inspect installed executables without installing or configuring anything."""

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: float = 30,
    ) -> None:
        self.which = which
        self.runner = runner
        self.timeout = timeout

    def _tool(self, name: str, *, required: bool) -> Capability:
        executable = self.which(name)
        return Capability(
            status="ready" if executable else "missing",
            required=required,
            executable=executable,
            message=None if executable else f"{name} is not installed",
        )

    def _agent_reach(self, executable: str) -> tuple[Capability, dict[str, Capability]]:
        try:
            completed = self.runner(
                ["agent-reach", "doctor", "--json"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return (
                Capability(
                    status="error",
                    executable=executable,
                    message=f"Agent Reach doctor failed: {type(error).__name__}",
                ),
                {},
            )
        if completed.returncode != 0:
            return (
                Capability(
                    status="error",
                    executable=executable,
                    message="Agent Reach doctor returned a non-zero exit status",
                ),
                {},
            )
        try:
            payload = _sanitize(json.loads(completed.stdout))
        except (json.JSONDecodeError, TypeError):
            return (
                Capability(
                    status="error",
                    executable=executable,
                    message="Agent Reach doctor did not return valid JSON",
                ),
                {},
            )

        rows = payload.get("channels", payload) if isinstance(payload, dict) else {}
        backends: dict[str, Capability] = {}
        for channel, row in rows.items():
            if not isinstance(row, dict) or "status" not in row:
                continue
            raw_status = str(row.get("status", "unavailable")).casefold()
            status: Literal["ready", "missing", "unavailable", "error"]
            if raw_status in {"ready", "ok", "available", "healthy"}:
                status = "ready"
            elif raw_status in {"missing", "not_installed"}:
                status = "missing"
            elif raw_status in {"error", "failed"}:
                status = "error"
            else:
                status = "unavailable"
            backends[str(channel)] = Capability(
                status=status,
                active_backend=row.get("active_backend"),
                message=row.get("message") or row.get("fix"),
                details={
                    str(key): value
                    for key, value in row.items()
                    if key not in {"status", "active_backend", "message", "fix"}
                },
            )
        return Capability(status="ready", executable=executable), backends

    def inspect(self) -> DoctorReport:
        tools = {
            "ffmpeg": self._tool("ffmpeg", required=True),
            "ffprobe": self._tool("ffprobe", required=True),
            "yt-dlp": self._tool("yt-dlp", required=False),
            "agent-reach": self._tool("agent-reach", required=False),
            "mcporter": self._tool("mcporter", required=False),
        }
        backends: dict[str, Capability] = {}
        agent_reach = tools["agent-reach"]
        if agent_reach.executable:
            tools["agent-reach"], backends = self._agent_reach(agent_reach.executable)
        return DoctorReport(tools=tools, backends=backends)


def run_doctor() -> DoctorReport:
    return CapabilityDoctor().inspect()
