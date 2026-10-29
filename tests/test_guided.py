from unittest.mock import Mock

from test_pipeline_cli import pipeline
from typer.testing import CliRunner

from vc_trace_collector.av import DiarizationResult, DiarizedTurn
from vc_trace_collector.cli import create_app
from vc_trace_collector.guided import PLATFORMS, STEPS, Guided
from vc_trace_collector.models import ApprovalStatus, MaterialRole, SourceDecision
from vc_trace_collector.storage import read_json


class Prompts:
    def __init__(self, **answers):
        self.answers = answers
        self.messages = []

    def text(self, key, message, default=""):
        return self.answers.get(key, default)

    def confirm(self, key, message):
        return self.answers.get(key, False)

    def select(self, key, message, options):
        return self.answers.get(key, options[0].value)

    def check(self, key, message, options):
        return self.answers.get(key, [])

    def show(self, message):
        self.messages.append(message)


def prepared(tmp_path, *, media=False, approve=True):
    local = tmp_path / ("talk.mp4" if media else "article.txt")
    local.write_bytes(b"I invest in customer-led businesses.")
    p = pipeline(tmp_path / "outputs")
    result = p.discover(name="Jane Roe", supplied_files=[local])
    candidate = next(
        c for c in result.source_plan.candidates if c.source_type == "supplied"
    )
    if approve:
        p.review(
            result.identity.slug,
            decisions=[
                SourceDecision(
                    candidate_id=candidate.candidate_id,
                    status=ApprovalStatus.APPROVED,
                    reason="fixture",
                    decided_by="human",
                    material_role=MaterialRole.SPOKEN_BY_TARGET
                    if media
                    else MaterialRole.AUTHORED_BY_TARGET,
                )
            ],
            reviewer="human",
            confirm_identity=True,
        )
    return p, candidate


def test_add_sources_appends_approved_and_skips_duplicates(tmp_path):
    p, _ = prepared(tmp_path)
    added = p.add_sources(
        "jane-roe",
        [
            "https://www.youtube.com/watch?v=abc",
            "https://www.youtube.com/watch?v=abc&utm_source=x",
        ],
        reviewer="human",
    )
    assert len(added) == 1
    assert added[0].approval_status == ApprovalStatus.APPROVED
    assert added[0].source_type == "youtube"
    assert p.add_sources("jane-roe", [added[0].url], reviewer="human") == []
    urls = [c.canonical_url for c in p._load_plan("jane-roe").candidates]
    assert urls.count(added[0].canonical_url) == 1


SKIP_ALL = [p.value for p in PLATFORMS]


def approve_all(state, title):
    for _ in state.rows:
        state.mark(ApprovalStatus.APPROVED)
        state.move(1)
    return "save"


def completed(p):
    return read_json(p.workspace("jane-roe") / "guided_state.json")["completed"]


def test_written_run_from_existing_workspace_completes(tmp_path):
    p, c = prepared(tmp_path, approve=False)
    ui = Prompts(skip=SKIP_ALL, confirm_identity=True)
    Guided(p, ui, triage=approve_all).run(investor="jane-roe")
    assert completed(p) == list(STEPS)
    # The summary must match the real verification result (this fixture's
    # document needs review, so it reports NOT verified).
    expected = "Verified ·" if p.verify("jane-roe").passed else "NOT verified ·"
    assert any(str(m).startswith(expected) for m in ui.messages)
    plan = {x.candidate_id: x for x in p._load_plan("jane-roe").candidates}
    assert plan[c.candidate_id].approval_status == ApprovalStatus.APPROVED


def test_resume_skips_completed_steps(tmp_path):
    from vc_trace_collector.storage import write_json

    p, _ = prepared(tmp_path)
    write_json(
        p.workspace("jane-roe") / "guided_state.json",
        {"completed": ["identity", "platforms", "triage"]},
    )
    p.search_source = Mock()
    triage = Mock()
    Guided(p, Prompts(resume=True), triage=triage).run(investor="jane-roe")
    p.search_source.assert_not_called()
    triage.assert_not_called()
    assert completed(p) == list(STEPS)


def test_unconfirmed_identity_stops_before_search(tmp_path):
    p, _ = prepared(tmp_path, approve=False)
    p.search_source = Mock()
    Guided(p, Prompts()).run(investor="jane-roe")
    p.search_source.assert_not_called()
    assert p._load_identity("jane-roe").resolution_status != "confirmed"


def test_quit_in_triage_saves_and_stops(tmp_path):
    p, _ = prepared(tmp_path, approve=False)
    p.fetch_source = Mock()

    def quit_after_reject(state, title):
        state.mark(ApprovalStatus.REJECTED)
        return "quit"

    Guided(
        p, Prompts(skip=SKIP_ALL, confirm_identity=True), triage=quit_after_reject
    ).run(investor="jane-roe")
    p.fetch_source.assert_not_called()
    statuses = [x.approval_status for x in p._load_plan("jane-roe").candidates]
    assert ApprovalStatus.REJECTED in statuses
    assert "triage" not in completed(p)


def test_triage_add_urls(tmp_path):
    p, _ = prepared(tmp_path)
    actions = iter(["add", "save"])
    Guided(
        p,
        Prompts(urls="https://www.youtube.com/watch?v=new"),
        triage=lambda s, t: next(actions),
    ).triage("jane-roe")
    urls = [x.canonical_url for x in p._load_plan("jane-roe").candidates]
    assert any("v=new" in u for u in urls)


def test_failing_source_is_reported_and_run_continues(tmp_path):
    p, _ = prepared(tmp_path)
    p.process = Mock(side_effect=RuntimeError("boom"))
    ui = Prompts(skip=SKIP_ALL)
    Guided(p, ui, triage=lambda s, t: "save").run(investor="jane-roe")
    assert any("boom" in str(m) for m in ui.messages)
    assert "finish" in completed(p)


def test_media_without_reference_is_skipped_in_process(tmp_path):
    p, c = prepared(tmp_path, media=True)
    p.fetch_source("jane-roe", candidate_ids={c.candidate_id})
    p.process = Mock(return_value=[])
    g = Guided(p, Prompts())
    g.process("jane-roe")
    p.process.assert_not_called()
    assert any("no voice reference" in f for f in g.failures)


def media_ready(tmp_path, ui):
    p, c = prepared(tmp_path, media=True)
    p.fetch_source("jane-roe", candidate_ids={c.candidate_id})
    p.media_probe = lambda path: 600.0

    def approved(*args, **kwargs):
        ui.answers["add_reference"] = False  # end the "add another?" loop

    p.approve_reference_voice = Mock(side_effect=approved)
    return p, c


def test_voice_path_a_approves_longest_turn_of_chosen_speaker(tmp_path, monkeypatch):
    ui = Prompts(add_reference=True, speaker="use:GUEST", confirm_voice=True)
    p, c = media_ready(tmp_path, ui)
    ui.answers["reference"] = c.candidate_id
    monkeypatch.setattr(
        "vc_trace_collector.guided.extract_audio_segment", lambda *a, **k: None
    )
    diarizer = Mock()
    diarizer.diarize.return_value = DiarizationResult(
        model="m",
        speaker_embeddings={},
        turns=[
            DiarizedTurn(start_seconds=0, end_seconds=5, speaker_label="HOST"),
            DiarizedTurn(start_seconds=5, end_seconds=65, speaker_label="GUEST"),
        ],
    )
    p._av_providers = Mock(return_value=(None, diarizer))
    Guided(p, ui, dependency_check=list).voice("jane-roe")
    kwargs = p.approve_reference_voice.call_args.kwargs
    assert (kwargs["start_seconds"], kwargs["end_seconds"]) == (5, 35)


def test_voice_path_b_uses_typed_range(tmp_path, monkeypatch):
    ui = Prompts(add_reference=True, range="01:00-01:20", confirm_voice=True)
    p, c = media_ready(tmp_path, ui)
    ui.answers["reference"] = c.candidate_id
    monkeypatch.setattr("vc_trace_collector.guided.play", lambda *a: None)
    p._av_providers = Mock(side_effect=RuntimeError("no model"))
    Guided(p, ui, dependency_check=list).voice("jane-roe")
    kwargs = p.approve_reference_voice.call_args.kwargs
    assert (kwargs["start_seconds"], kwargs["end_seconds"]) == (60, 80)


def test_voice_missing_dependencies_skips_without_models(tmp_path):
    ui = Prompts()
    p, _ = media_ready(tmp_path, ui)
    p._av_providers = Mock()
    Guided(p, ui, dependency_check=lambda: ["ffmpeg"]).voice("jane-roe")
    p._av_providers.assert_not_called()
    assert any("ffmpeg" in str(m) for m in ui.messages)


def test_wizard_cli_rejects_noninteractive_terminal_without_work(tmp_path):
    factory = Mock()
    result = CliRunner().invoke(
        create_app(factory), ["wizard", "--investor", "jane-roe"]
    )
    assert result.exit_code == 2
    assert "interactive terminal" in result.output
    factory.assert_not_called()
