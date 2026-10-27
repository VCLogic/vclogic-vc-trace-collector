import pytest

from vc_trace_collector.av import DiarizedTurn
from vc_trace_collector.voice import parse_range, reference_interval, speakers


def turn(start, end, label):
    return DiarizedTurn(start_seconds=start, end_seconds=end, speaker_label=label)


def test_speakers_sorted_by_talk_time_with_longest_turn():
    rows = speakers(
        [turn(0, 10, "A"), turn(10, 70, "B"), turn(70, 75, "A"), turn(75, 80, "B")]
    )
    assert [(label, total) for label, total, _ in rows] == [("B", 65), ("A", 15)]
    assert rows[0][2].start_seconds == 10


def test_reference_interval_caps_at_thirty_seconds():
    assert reference_interval(turn(10, 70, "B")) == (10, 40)
    assert reference_interval(turn(5, 12, "B")) == (5, 12)


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
            "michael-hyatt",
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
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    voice = read_jsonl(
        p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
    )[0]
    assert voice["artifact_id"]


def test_voice_candidate_returns_id_and_local_media(tmp_path):
    from test_guided import prepared

    from vc_trace_collector.voice import voice_candidate
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    item = next(
        i
        for i in source_items(p, "michael-hyatt")
        if i.candidate.candidate_id == c.candidate_id
    )
    cid, media = voice_candidate(p, "michael-hyatt", item, reviewer="human")
    assert cid.startswith("voice")
    workspace = p.workspace("michael-hyatt").resolve()
    assert media.is_file() and media.is_relative_to(workspace)
