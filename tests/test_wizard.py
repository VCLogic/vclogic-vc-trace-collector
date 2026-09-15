from pathlib import Path
from unittest.mock import Mock

import pytest
from test_pipeline_cli import pipeline
from typer.testing import CliRunner

from vc_trace_collector.cli import create_app
from vc_trace_collector.models import ApprovalStatus, MaterialRole, SourceDecision
from vc_trace_collector.storage import read_json, write_json


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

    def pick_sources(self, items, purpose):
        self.messages.extend(items)
        return self.answers.get(purpose, [])

    def show(self, message):
        self.messages.append(message)


def prepared(tmp_path, *, media=False, approve=True):
    local = tmp_path / ("talk.mp4" if media else "article.txt")
    local.write_bytes(b"I invest in customer-led businesses.")
    p = pipeline(tmp_path / "outputs")
    result = p.discover(name="Michael Hyatt", supplied_files=[local])
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


def test_wizard_cli_rejects_noninteractive_terminal_without_work(tmp_path):
    factory = Mock()
    result = CliRunner().invoke(
        create_app(factory),
        ["wizard", "--stage", "download", "--investor", "michael-hyatt"],
    )
    assert result.exit_code == 2
    assert "interactive terminal" in result.output
    factory.assert_not_called()


def test_cancelled_review_does_not_change_plan(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p, c = prepared(tmp_path, approve=False)
    before = p._load_plan("michael-hyatt")
    ui = Prompts(review=[c.candidate_id], decision="approved", confirm_review=False)
    Wizard(p, ui).review("michael-hyatt")
    assert p._load_plan("michael-hyatt") == before


def test_review_preserves_unselected_candidates(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p, c = prepared(tmp_path, approve=False)
    ui = Prompts(
        review=[c.candidate_id],
        decision="approved",
        role="authored_by_target",
        confirm_review=True,
    )
    Wizard(p, ui).review("michael-hyatt")
    assert (
        next(
            x
            for x in p._load_plan("michael-hyatt").candidates
            if x.candidate_id == c.candidate_id
        ).approval_status
        == "approved"
    )


def test_download_never_processes_and_rejects_unapproved_selection(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p, c = prepared(tmp_path, approve=False)
    p.fetch_source = Mock()
    p.process = Mock()
    with pytest.raises(ValueError, match="selection"):
        Wizard(p, Prompts(download=[c.candidate_id], confirm_download=True)).download(
            "michael-hyatt"
        )
    p.fetch_source.assert_not_called()
    p.process.assert_not_called()


def test_download_delegates_approved_subset_only(tmp_path):
    from vc_trace_collector.collectors import CollectionResult
    from vc_trace_collector.wizard import Wizard

    p, c = prepared(tmp_path)
    p.fetch_source = Mock(return_value=CollectionResult(collected=1))
    p.process = Mock()
    Wizard(p, Prompts(download=[c.candidate_id], confirm_download=True)).download(
        "michael-hyatt"
    )
    p.fetch_source.assert_called_once_with(
        "michael-hyatt", candidate_ids={c.candidate_id}
    )
    p.process.assert_not_called()


def test_written_processing_needs_no_voice_or_download(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p, c = prepared(tmp_path)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    p.fetch_source = Mock()
    p.approve_reference_voice = Mock()
    ui = Prompts(process=[c.candidate_id], confirm_process=True)
    Wizard(p, ui).process("michael-hyatt")
    assert (p.workspace("michael-hyatt") / "processed/documents.jsonl").exists()
    p.fetch_source.assert_not_called()
    p.approve_reference_voice.assert_not_called()


def test_media_without_reference_stops_before_models(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    p.process = Mock()
    p.fetch_source = Mock()
    ui = Prompts(process=[c.candidate_id], reference_action="later")
    Wizard(p, ui, dependency_check=list).process("michael-hyatt")
    p.process.assert_not_called()
    p.fetch_source.assert_not_called()


def test_missing_raw_file_is_not_processable(tmp_path):
    from vc_trace_collector.wizard import Wizard
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    item = next(
        x
        for x in source_items(p, "michael-hyatt")
        if x.candidate.candidate_id == c.candidate_id
    )
    Path(item.paths[0]).unlink()
    assert not next(
        x
        for x in source_items(p, "michael-hyatt")
        if x.candidate.candidate_id == c.candidate_id
    ).downloaded
    with pytest.raises(ValueError, match="selection"):
        Wizard(p, Prompts(process=[c.candidate_id])).process("michael-hyatt")


def test_discovery_resumes_existing_identity_without_overwriting(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p, _ = prepared(tmp_path)
    before = read_json(p.workspace("michael-hyatt") / "identity/resolved_identity.json")
    p.discover = Mock(side_effect=AssertionError("must not rediscover"))
    Wizard(p, Prompts()).discover(name="Michael Hyatt")
    assert (
        read_json(p.workspace("michael-hyatt") / "identity/resolved_identity.json")
        == before
    )


def test_path_traversal_rejected(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p = pipeline(tmp_path)
    with pytest.raises(ValueError, match="slug"):
        Wizard(p, Prompts()).run(stage="download", investor="../outside")


def test_stale_confirmation_does_not_authorize_different_identity(tmp_path):
    from vc_trace_collector.wizard import Wizard

    p, c = prepared(tmp_path)
    path = p.workspace("michael-hyatt") / "identity/resolved_identity.json"
    identity = read_json(path)
    identity["resolution_status"] = "provisional"
    write_json(path, identity)
    p.fetch_source = Mock()
    Wizard(p, Prompts(download=[c.candidate_id], confirm_download=True)).download(
        "michael-hyatt"
    )
    p.fetch_source.assert_not_called()


def test_profile_options_are_not_merged_and_no_download_before_confirmation(tmp_path):
    from vc_trace_collector.discovery import SearchResult
    from vc_trace_collector.wizard_profiles import resolve_profile

    p = pipeline(tmp_path)
    provider = Mock(provider_name="fixture", spec=["search", "provider_name"])
    provider.search.return_value = [
        SearchResult(
            url=f"https://example.test/{i}",
            title=title,
            rank=i + 1,
            query="fixture",
            provider="fixture",
        )
        for i, title in enumerate(["Alex Kim investor at A", "Alex Kim investor at B"])
    ]
    ui = Prompts(
        profile="https://example.test/1",
        confirm_profile=False,
        confirm_profile_search=True,
    )
    assert resolve_profile(p, ui, "Alex Kim", provider=provider) is None
    data = read_json(tmp_path / "alex-kim/discovery/profile_options.json")
    assert len(data["results"]) == 2
    assert not (tmp_path / "alex-kim/identity/resolved_identity.json").exists()


def test_profile_search_budget_prevents_provider_call(tmp_path):
    from vc_trace_collector.audit import BudgetExceeded
    from vc_trace_collector.wizard_profiles import resolve_profile

    p = pipeline(tmp_path)
    provider = Mock(provider_name="fixture", spec=["search", "provider_name"])
    with pytest.raises(BudgetExceeded):
        resolve_profile(
            p,
            Prompts(max_cost="0", search_cost="1", confirm_profile_search=True),
            "Alex Kim",
            provider=provider,
        )
    provider.search.assert_not_called()


def test_reference_local_file_is_only_staged_for_download(tmp_path):
    from vc_trace_collector.wizard_reference import stage_local_reference

    p, _ = prepared(tmp_path)
    voice = tmp_path / "reference.wav"
    voice.write_bytes(b"voice")
    p.fetch_source = Mock()
    p.process = Mock()
    cid = stage_local_reference(p, "michael-hyatt", voice, "human:test")
    c = next(
        c for c in p._load_plan("michael-hyatt").candidates if c.candidate_id == cid
    )
    assert c.material_role == "reference_voice"
    assert c.approval_status == "approved"
    assert c.canonical_url == voice.as_uri()
    p.fetch_source.assert_not_called()
    p.process.assert_not_called()


def test_reference_invalid_interval_never_calls_embedding(tmp_path):
    from vc_trace_collector.storage import read_jsonl
    from vc_trace_collector.wizard_reference import prepare_reference

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    voice = read_jsonl(
        p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
    )[0]
    p.approve_reference_voice = Mock()
    ui = Prompts(
        reference_action="recording",
        reference=voice["candidate_id"],
        start="90",
        end="20",
        confirm_voice=True,
    )
    with pytest.raises(ValueError, match="interval"):
        prepare_reference(p, ui, "michael-hyatt", list)
    p.approve_reference_voice.assert_not_called()


def test_new_discovery_requires_identity_confirmation_before_platform_search(
    tmp_path, monkeypatch
):
    from vc_trace_collector.config import RunConfig
    from vc_trace_collector.wizard import Wizard

    p = pipeline(tmp_path / "outputs")
    monkeypatch.setattr(
        "vc_trace_collector.wizard_profiles.resolve_profile",
        lambda *a: (
            "https://example.test/profile",
            RunConfig(name="Michael Hyatt", public_search_enabled=False),
        ),
    )
    p.search_source = Mock()
    p.fetch_source = Mock()
    p.process = Mock()
    Wizard(p, Prompts(platforms=["youtube"], confirm_search=True)).discover(
        name="Michael Hyatt"
    )
    p.search_source.assert_not_called()
    p.fetch_source.assert_not_called()
    p.process.assert_not_called()


def test_failed_download_status_is_visible_without_artifacts(tmp_path):
    from vc_trace_collector.storage import write_jsonl
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path)
    write_jsonl(
        p.workspace("michael-hyatt") / "processed/collection_candidate_outcomes.jsonl",
        [
            {
                "candidate_id": c.candidate_id,
                "status": "failed",
                "reason": "HTTP timeout",
            }
        ],
    )
    item = next(
        x
        for x in source_items(p, "michael-hyatt")
        if x.candidate.candidate_id == c.candidate_id
    )
    assert item.collection_status == "failed"
    assert not item.downloaded


def test_reference_default_uses_diarization_embedding_space(tmp_path):
    from vc_trace_collector.storage import read_jsonl
    from vc_trace_collector.wizard_reference import prepare_reference

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    voice = read_jsonl(
        p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
    )[0]
    p.media_probe = lambda path: 120.0
    p.approve_reference_voice = Mock()
    ui = Prompts(
        reference_action="recording",
        reference=voice["candidate_id"],
        start="0",
        end="30",
        confirm_voice=True,
    )
    prepare_reference(p, ui, "michael-hyatt", list)
    assert p.approve_reference_voice.call_args.kwargs["embedding_model"] == ""


def test_reference_interval_cannot_exceed_recording(tmp_path):
    from vc_trace_collector.storage import read_jsonl
    from vc_trace_collector.wizard_reference import prepare_reference

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    voice = read_jsonl(
        p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
    )[0]
    p.media_probe = lambda path: 60.0
    p.approve_reference_voice = Mock()
    ui = Prompts(
        reference_action="recording",
        reference=voice["candidate_id"],
        start="10",
        end="90",
        confirm_voice=True,
    )
    with pytest.raises(ValueError, match="duration"):
        prepare_reference(p, ui, "michael-hyatt", list)
    p.approve_reference_voice.assert_not_called()


def test_source_details_include_retrieved_evidence(tmp_path):
    from vc_trace_collector.wizard_models import source_items

    p = pipeline(tmp_path)
    result = p.discover(
        name="Michael Hyatt",
        known_profile_url="https://www.thepitch.show/investors/michael-hyatt",
    )
    items = source_items(p, result.identity.slug)
    assert any(
        item.evidence and any(e.get("excerpt") for e in item.evidence) for item in items
    )


def test_profile_preserves_firm_without_changing_name_folder(tmp_path, monkeypatch):
    from vc_trace_collector.config import RunConfig
    from vc_trace_collector.wizard import Wizard

    p = pipeline(tmp_path)
    monkeypatch.setattr(
        "vc_trace_collector.wizard_profiles.resolve_profile",
        lambda *a: (
            "https://example.test/profile",
            RunConfig(
                name="Michael Hyatt", firm="Known Firm", public_search_enabled=False
            ),
        ),
    )
    Wizard(p, Prompts(confirm_identity=True)).discover(name="Michael Hyatt")
    identity = p._load_identity("michael-hyatt")
    assert any(a.firm == "Known Firm" for a in identity.affiliations)


def test_stage_reference_cli_records_plan_without_download(tmp_path):
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


def test_process_cli_can_select_written_candidate_without_processing_others(tmp_path):
    p, c = prepared(tmp_path)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    p.process = Mock(return_value=[])
    result = CliRunner().invoke(
        create_app(lambda output: p),
        ["process", "--investor", "michael-hyatt", "--candidate-id", c.candidate_id],
    )
    assert result.exit_code == 0, result.output
    assert p.process.call_args.kwargs["candidate_ids"] == {c.candidate_id}


def test_supplied_video_has_reference_backlink_after_download(tmp_path):
    from vc_trace_collector.storage import read_jsonl

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    voice = read_jsonl(
        p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
    )[0]
    assert voice["artifact_id"]
