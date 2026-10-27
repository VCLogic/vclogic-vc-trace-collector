from unittest.mock import Mock

import pytest
from test_wizard import Prompts, prepared


def test_accept_all_pending_needs_explicit_confirmation(tmp_path):
    from vc_trace_collector.dashboard import Dashboard

    p, _candidate = prepared(tmp_path, approve=False)
    ui = Prompts(review_mode="all", confirm_bulk=False)
    Dashboard(p, ui).review("michael-hyatt")
    assert p._load_plan("michael-hyatt").candidates[0].approval_status == "pending"


def test_accept_all_pending_preserves_role_and_rejections(tmp_path):
    from vc_trace_collector.dashboard import Dashboard

    p, candidate = prepared(tmp_path, approve=False)
    original = candidate.material_role
    ui = Prompts(review_mode="all", confirm_bulk=True, reviewer="human:test")
    Dashboard(p, ui).review("michael-hyatt")
    result = p._load_plan("michael-hyatt").candidates[0]
    assert result.approval_status == "approved"
    assert result.material_role == original


def test_download_cancel_never_launches_job(tmp_path, monkeypatch):
    from vc_trace_collector.dashboard import Dashboard

    p, _c = prepared(tmp_path)
    launch = Mock()
    monkeypatch.setattr("vc_trace_collector.dashboard.launch_job", launch)
    Dashboard(p, Prompts(selection="all", confirm_job=False)).download("michael-hyatt")
    launch.assert_not_called()


def test_ui_sanitizes_terminal_control_sequences():
    from vc_trace_collector.dashboard_ui import display_text

    assert "\x1b" not in display_text("remote\x1b[31m title")
    assert "hf_abcdefgh12345" not in display_text("hf_abcdefgh12345")


@pytest.mark.parametrize("missing_link", [False, True])
def test_reference_registers_acquired_recording_with_human_interval(
    tmp_path, monkeypatch, missing_link
):
    from vc_trace_collector.dashboard import Dashboard
    from vc_trace_collector.storage import read_jsonl, write_jsonl

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    if missing_link:
        voice_path = (
            p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
        )
        rows = read_jsonl(voice_path)
        assert rows
        for row in rows:
            row["artifact_id"] = None
        write_jsonl(voice_path, rows)
    p.media_probe = lambda path: 200
    monkeypatch.setattr("vc_trace_collector.dashboard.missing_av_dependencies", list)
    ui = Prompts(reference=c.candidate_id, start="1:11", end="2:15", confirm_voice=True)
    dashboard = Dashboard(p, ui)
    dashboard.launch = Mock()
    dashboard.reference("michael-hyatt")
    assert dashboard.launch.call_args.kwargs["start_seconds"] == 71
    assert dashboard.launch.call_args.kwargs["end_seconds"] == 135
    rows = read_jsonl(
        p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
    )
    assert any(
        r["source_candidate_id"] == c.candidate_id and r["artifact_id"] for r in rows
    )


def test_fullscreen_escape_returns_without_action(monkeypatch):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.widgets import Label

    from vc_trace_collector.dashboard_ui import DashboardPrompts

    ui = DashboardPrompts.__new__(DashboardPrompts)
    ui.breadcrumb = "Fixture"

    async def check():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            app = ui.application("Dashboard", Label("Test"), [])
            pipe.send_text("\x1b")
            assert await app.run_async() is None

    asyncio.run(check())


def test_incompatible_reference_stops_before_processing(tmp_path, monkeypatch):
    from vc_trace_collector.dashboard import Dashboard
    from vc_trace_collector.storage import write_json

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    write_json(
        p.workspace("michael-hyatt") / "identity/reference_voice_profile.json",
        {"status": "verified_human", "embedding_model": "different-model"},
    )
    monkeypatch.setattr("vc_trace_collector.dashboard.missing_av_dependencies", list)
    ui = Prompts(selection="all", confirm_job=True)
    d = Dashboard(p, ui)
    d.launch = Mock()
    d.process("michael-hyatt")
    d.launch.assert_not_called()


def test_local_reference_option_stages_without_implicit_download(tmp_path, monkeypatch):
    from vc_trace_collector.dashboard import Dashboard

    p, _ = prepared(tmp_path)
    local = tmp_path / "reference.wav"
    local.write_bytes(b"fixture")
    monkeypatch.setattr("vc_trace_collector.dashboard.missing_av_dependencies", list)
    ui = Prompts(
        reference="local",
        reference_path=str(local),
        confirm_local=True,
        confirm_local_download=False,
    )
    d = Dashboard(p, ui)
    d.launch = Mock()
    d.reference("michael-hyatt")
    assert any(
        c.material_role == "reference_voice"
        for c in p._load_plan("michael-hyatt").candidates
    )
    d.launch.assert_not_called()


def test_source_picker_filter_preserves_selection_and_shows_evidence(tmp_path):
    from vc_trace_collector.dashboard_ui import SourcePicker
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path)
    picker = SourcePicker(source_items(p, "michael-hyatt"))
    assert picker.body.preferred_height(80, 24).min <= 16
    picker.table.current_values = [c.candidate_id]
    assert c.canonical_url in picker.detail()
    picker.search.text = "no matching item"
    assert picker.selected() == [c.candidate_id]
    assert "No matching" in picker.detail()
    picker.search.text = ""
    assert c.candidate_id in picker.table.current_values


def test_picker_full_details_preserves_source_provenance(tmp_path):
    from vc_trace_collector.dashboard_ui import DashboardPrompts
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path)
    ui = object.__new__(DashboardPrompts)
    ui.application = Mock(
        side_effect=[Mock(run=lambda: "details"), Mock(run=lambda: [c.candidate_id])]
    )
    ui.show = Mock()
    items = source_items(p, "michael-hyatt")
    assert ui.pick_sources(items, "review") == [c.candidate_id]
    ui.show.assert_called_once_with(items[0].model_dump(mode="json"))


def test_buttons_fit_narrow_terminal(monkeypatch):
    import os

    from prompt_toolkit.widgets import Label

    from vc_trace_collector.dashboard_ui import DashboardPrompts

    monkeypatch.setattr(
        "vc_trace_collector.dashboard_ui.shutil.get_terminal_size",
        lambda: os.terminal_size((40, 24)),
    )
    ui = object.__new__(DashboardPrompts)
    ui.breadcrumb = "Review"
    app = ui.application(
        "Platforms",
        Label("Choose platforms"),
        [
            (label, lambda: None)
            for label in ["Continue", "Select visible", "Clear", "Details", "Back"]
        ],
    )
    assert app.layout.container.preferred_width(40).min <= 40
