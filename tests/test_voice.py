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
