"""Read-only checks for local collection and Agent Reach capabilities."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .audit import redact


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
        # Agent Reach 1.6+ always prints JSON and rejects --json; 1.5 needs --json.
        payload = None
        for command in (["agent-reach", "doctor"], ["agent-reach", "doctor", "--json"]):
            try:
                completed = self.runner(
                    command,
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
                continue
            try:
                payload = json.loads(completed.stdout)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                break
        if not isinstance(payload, dict):
            return (
                Capability(
                    status="error",
                    executable=executable,
                    message="Agent Reach doctor did not return valid JSON",
                ),
                {},
            )
        payload = redact(payload)
        if payload.get("ok") is False:
            error = payload.get("error") or {}
            return (
                Capability(
                    status="error",
                    executable=executable,
                    message=str(
                        error.get("message") or "Agent Reach doctor reported failure"
                    ),
                ),
                {},
            )
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("channels"), list):
            rows = {
                str(row["channel"]): row
                for row in result["channels"]
                if isinstance(row, dict) and "channel" in row
            }
        else:
            rows = payload.get("channels", payload)
        backends: dict[str, Capability] = {}
        for channel, row in rows.items():
            if not isinstance(row, dict) or "status" not in row:
                continue
            raw_status = str(row.get("status", "unavailable")).casefold()
            status: Literal["ready", "missing", "unavailable", "error"]
            if raw_status in {"ready", "ok", "available", "healthy"}:
                status = "ready"
            elif raw_status in {"missing", "not_installed", "not_added", "needs_setup"}:
                status = "missing"
            elif raw_status in {"error", "failed"}:
                status = "error"
            else:
                status = "unavailable"
            backends[str(channel)] = Capability(
                status=status,
                active_backend=row.get("active_backend"),
                message=row.get("message")
                or row.get("fix")
                or row.get("hint")
                or "; ".join(map(str, row.get("problems") or []))
                or None,
                details={
                    str(key): value
                    for key, value in row.items()
                    if key not in {"status", "active_backend", "message", "fix", "hint"}
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
