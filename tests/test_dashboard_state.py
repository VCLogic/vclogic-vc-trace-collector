import pytest
from test_wizard import prepared


def test_summary_distinguishes_discovered_and_downloaded(tmp_path):
    from vc_trace_collector.dashboard_state import summarize

    p, c = prepared(tmp_path)
    before = summarize(p, "michael-hyatt")
    assert before["name"] == "Michael Hyatt"
    assert before["downloaded"] == 0
    assert before["next"] == "download"
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    after = summarize(p, "michael-hyatt")
    assert after["downloaded"] == 1
    assert after["next"] == "process"


@pytest.mark.parametrize(
    "value,seconds", [("1:11", 71), ("2:15", 135), ("1:02:03", 3723), ("71.5", 71.5)]
)
def test_timestamp(value, seconds):
    from vc_trace_collector.dashboard_state import parse_timestamp

    assert parse_timestamp(value) == seconds


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "1:60", "1:-2", "1:2:3:4", ""])
def test_invalid_timestamp(value):
    from vc_trace_collector.dashboard_state import parse_timestamp

    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_reference_suggestions_require_downloaded_approved_media(tmp_path):
    from vc_trace_collector.dashboard_state import reference_suggestions
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path, media=True)
    assert (
        reference_suggestions(source_items(p, "michael-hyatt"), "Michael Hyatt") == []
    )
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    assert (
        reference_suggestions(source_items(p, "michael-hyatt"), "Michael Hyatt")[
            0
        ].candidate.candidate_id
        == c.candidate_id
    )


def test_incomplete_workspace_is_visible(tmp_path):
    from vc_trace_collector.dashboard_state import workspaces

    p, _ = prepared(tmp_path)
    (p.workspace("michael-hyatt") / "identity/resolved_identity.json").unlink()
    assert workspaces(p)[0]["next"] == "repair"


def test_not_collected_is_not_processed(tmp_path):
    from vc_trace_collector.dashboard_state import summarize
    from vc_trace_collector.storage import write_jsonl

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    write_jsonl(
        p.workspace("michael-hyatt") / "processed/av_candidate_outcomes.jsonl",
        [{"candidate_id": c.candidate_id, "status": "not_collected"}],
    )
    row = summarize(p, "michael-hyatt")
    assert row["processed"] == 0
    assert row["next"] == "reference"


def test_reference_only_not_pending_processing(tmp_path):
    from vc_trace_collector.dashboard_state import summarize
    from vc_trace_collector.models import SourceDecision

    p, c = prepared(tmp_path, media=True)
    p.review(
        "michael-hyatt",
        decisions=[
            SourceDecision(
                candidate_id=c.candidate_id,
                status="approved",
                material_role="reference_voice",
                decided_by="human",
                reason="reference",
            )
        ],
        reviewer="human",
        confirm_identity=True,
    )
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    assert summarize(p, "michael-hyatt")["next"] == "results"
