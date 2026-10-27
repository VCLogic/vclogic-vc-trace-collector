import asyncio
from unittest.mock import Mock

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_wizard import Prompts, prepared


def test_browser_enter_inspects_without_mutating(tmp_path):
    from vc_trace_collector.dashboard_ui import DashboardPrompts
    from vc_trace_collector.inventory import inventory
    from vc_trace_collector.inventory_ui import InventoryBrowser

    p, c = prepared(tmp_path)
    rows = inventory(p, "michael-hyatt")
    ui = object.__new__(DashboardPrompts)
    ui.breadcrumb = "Michael Hyatt"

    async def run():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            browser = InventoryBrowser(ui, rows, "Michael Hyatt")
            pipe.send_text("\r")
            return await browser.app.run_async()

    assert asyncio.run(run()) == ("details", c.candidate_id)
    assert not rows[0].item.downloaded


def test_browser_shortcut_and_escape(tmp_path):
    from vc_trace_collector.dashboard_ui import DashboardPrompts
    from vc_trace_collector.inventory import inventory
    from vc_trace_collector.inventory_ui import InventoryBrowser

    p, _ = prepared(tmp_path)
    ui = object.__new__(DashboardPrompts)
    ui.breadcrumb = "Michael Hyatt"

    async def run(keys):
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            browser = InventoryBrowser(
                ui, inventory(p, "michael-hyatt"), "Michael Hyatt"
            )
            pipe.send_text(keys)
            return await browser.app.run_async()

    assert asyncio.run(run("p")) == ("process_media", None)
    assert asyncio.run(run("\x1b")) is None


def test_browser_search_and_filter(tmp_path):
    from vc_trace_collector.dashboard_ui import DashboardPrompts
    from vc_trace_collector.inventory import inventory
    from vc_trace_collector.inventory_ui import InventoryBrowser

    p, _ = prepared(tmp_path)
    ui = object.__new__(DashboardPrompts)
    ui.breadcrumb = "Michael Hyatt"
    browser = InventoryBrowser(ui, inventory(p, "michael-hyatt"), "Michael Hyatt")
    assert len(browser.visible) == 1
    browser.search.text = "nonexistent"
    assert browser.visible == []
    browser.search.text = ""
    browser.filter_name = "Ready to process"
    browser.refresh()
    assert browser.visible == []


def test_investor_opens_inventory_even_during_job(tmp_path, monkeypatch):
    from vc_trace_collector.dashboard import Dashboard

    p, _ = prepared(tmp_path)
    ui = Prompts()
    ui.busy = lambda message, fn: fn()
    browser = Mock()
    browser.return_value.run.return_value = None
    monkeypatch.setattr("vc_trace_collector.inventory_ui.InventoryBrowser", browser)
    d = Dashboard(p, ui)
    d.is_busy = Mock(return_value=True)
    d.investor("michael-hyatt")
    d.is_busy.assert_not_called()
    browser.assert_called_once()


def test_process_downloaded_recording_despite_another_missing_download(
    tmp_path, monkeypatch
):
    from vc_trace_collector.dashboard import Dashboard
    from vc_trace_collector.dashboard_state import summarize
    from vc_trace_collector.storage import write_json

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    plan = p._load_plan("michael-hyatt")
    plan.candidates.append(
        plan.candidates[0].model_copy(
            update={
                "candidate_id": "candidate:missing",
                "canonical_url": "https://example.org/missing.mp4",
            }
        )
    )
    write_json(p.workspace("michael-hyatt") / "discovery/source_plan.json", plan)
    write_json(
        p.workspace("michael-hyatt") / "identity/reference_voice_profile.json",
        {
            "status": "verified_human",
            "embedding_model": "pyannote/speaker-diarization-3.1",
            "profile_id": "fixture",
        },
    )
    monkeypatch.setattr("vc_trace_collector.dashboard.missing_av_dependencies", list)
    ui = Prompts(selection="all", confirm_job=True)
    d = Dashboard(p, ui)
    d.launch = Mock()
    assert summarize(p, "michael-hyatt")["missing"] == 1
    assert summarize(p, "michael-hyatt")["ready_to_process"] == 1
    d.process("michael-hyatt", media_only=True)
    assert d.launch.call_args.args == ("michael-hyatt", "process", [c.candidate_id])
