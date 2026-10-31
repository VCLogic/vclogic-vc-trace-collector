import pytest

from vc_trace_collector.voice import clock, parse_range


def test_clock_matches_parse_range():
    assert clock(190) == "03:10"
    assert clock(3610.7) == "1:00:10"
    assert parse_range(f"{clock(190)}-{clock(3610)}") == (190, 3610)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("03:10-03:40", (190, 220)),
        ("1:00:00 - 1:00:30", (3600, 3630)),
        ("5-9.5", (5, 9.5)),
    ],
)
def test_parse_range(text, expected):
    assert parse_range(text) == expected


@pytest.mark.parametrize("text", ["", "03:40-03:10", "abc-def", "10", "1..2-3"])
def test_parse_range_rejects_bad_input(text):
    with pytest.raises(ValueError):
        parse_range(text)


def test_stage_reference_cli_records_plan_without_download(tmp_path):
    from unittest.mock import Mock

    from test_guided import prepared
    from typer.testing import CliRunner

    from vc_trace_collector.cli import create_app

    p, _ = prepared(tmp_path)
    local = tmp_path / "sample.wav"
    local.write_bytes(b"sample")
    p.fetch_source = Mock()
    result = CliRunner().invoke(
        create_app(lambda output: p),
        [
            "stage-reference",
            "--investor",
            "jane-roe",
            "--file",
            str(local),
            "--reviewer",
            "human:test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "candidate:" in result.output
    p.fetch_source.assert_not_called()


def test_supplied_video_has_reference_backlink_after_download(tmp_path):
    from test_guided import prepared

    from vc_trace_collector.storage import read_jsonl

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("jane-roe", candidate_ids={c.candidate_id})
    voice = read_jsonl(
        p.workspace("jane-roe") / "identity/reference_voice_candidates.jsonl"
    )[0]
    assert voice["artifact_id"]


def test_voice_candidate_returns_id_and_local_media(tmp_path):
    from test_guided import prepared

    from vc_trace_collector.voice import voice_candidate
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("jane-roe", candidate_ids={c.candidate_id})
    item = next(
        i
        for i in source_items(p, "jane-roe")
        if i.candidate.candidate_id == c.candidate_id
    )
    cid, media = voice_candidate(p, "jane-roe", item, reviewer="human")
    assert cid.startswith("voice")
    workspace = p.workspace("jane-roe").resolve()
    assert media.is_file() and media.is_relative_to(workspace)
