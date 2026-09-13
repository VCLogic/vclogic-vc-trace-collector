import json
import subprocess

from typer.testing import CliRunner

import vc_trace_collector.cli as cli_module
from vc_trace_collector.capabilities import CapabilityDoctor, DoctorReport
from vc_trace_collector.cli import create_app


def test_doctor_reports_missing_optional_executables() -> None:
    doctor = CapabilityDoctor(which=lambda _name: None)

    report = doctor.inspect()

    assert report.tools["agent-reach"].status == "missing"
    assert report.tools["yt-dlp"].status == "missing"
    assert report.tools["ffmpeg"].required is True
    assert report.tools["agent-reach"].required is False


def test_doctor_parses_agent_reach_backends_and_redacts_secrets() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "youtube": {"status": "ready", "active_backend": "yt-dlp"},
                    "twitter": {
                        "status": "ready",
                        "active_backend": "twitter-cli",
                        "cookie": "secret-cookie",
                    },
                }
            ),
            stderr="",
        )

    doctor = CapabilityDoctor(
        which=lambda name: f"/usr/bin/{name}", runner=runner
    )
    report = doctor.inspect()

    assert calls[0] == ["agent-reach", "doctor", "--json"]
    assert report.backends["youtube"].active_backend == "yt-dlp"
    assert report.backends["twitter"].details["cookie"] == "[REDACTED]"


def test_cli_doctor_emits_json(monkeypatch) -> None:
    report = DoctorReport()
    monkeypatch.setattr(cli_module, "run_doctor", lambda: report)

    result = CliRunner().invoke(create_app(), ["doctor", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema_version"] == "1.0"
