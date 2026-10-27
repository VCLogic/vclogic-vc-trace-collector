import pytest
from test_wizard import prepared

from vc_trace_collector.storage import write_jsonl


def test_written_sources_have_no_audio_stages(tmp_path):
    from vc_trace_collector.inventory import inventory

    p, c = prepared(tmp_path)
    row = inventory(p, "michael-hyatt")[0]
    assert row.candidate_id == c.candidate_id
    assert row.transcription == row.diarization == "n/a"
    assert row.download == "missing"
    assert row.approval == "approved"


def test_media_readiness_does_not_require_all_downloads(tmp_path):
    from vc_trace_collector.inventory import inventory

    p, c = prepared(tmp_path, media=True)
    assert not inventory(p, "michael-hyatt")[0].ready
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    row = inventory(p, "michael-hyatt")[0]
    assert row.ready
    assert row.transcription == row.diarization == "pending"
    assert row.attribution == "pending"


def test_completed_stages_and_uncertain_speaker_are_separate(tmp_path):
    from vc_trace_collector.inventory import inventory

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    workspace = p.workspace("michael-hyatt")
    write_jsonl(
        workspace / "processed/av_attribution_results.jsonl",
        [
            {
                "candidate_id": c.candidate_id,
                "result": {
                    "transcript": {"method": "whisper"},
                    "diarization_model": "fixture",
                    "attribution": {
                        "status": "uncertain",
                        "score": 0.59,
                        "minimum_score": 0.60,
                    },
                },
            }
        ],
    )
    write_jsonl(
        workspace / "processed/av_candidate_outcomes.jsonl",
        [
            {
                "candidate_id": c.candidate_id,
                "status": "review_required",
                "reason": "Target-speaker attribution requires human review",
            }
        ],
    )
    row = inventory(p, "michael-hyatt")[0]
    assert row.transcription == row.diarization == "complete"
    assert row.attribution == "uncertain"
    assert not row.ready
    assert "0.59" in row.details()
    assert "human review" in row.details()


def test_processing_failure_does_not_invent_failed_stage(tmp_path):
    from vc_trace_collector.inventory import inventory

    p, c = prepared(tmp_path, media=True)
    write_jsonl(
        p.workspace("michael-hyatt") / "processed/av_candidate_outcomes.jsonl",
        [
            {
                "candidate_id": c.candidate_id,
                "status": "failed",
                "reason": "Cannot compare a zero-length embedding vector",
            }
        ],
    )
    row = inventory(p, "michael-hyatt")[0]
    assert row.processing == "failed"
    assert row.transcription == row.diarization == "unknown"
    assert "zero-length" in row.details()


def test_model_match_is_not_human_verification(tmp_path):
    from vc_trace_collector.inventory import inventory

    p, c = prepared(tmp_path, media=True)
    write_jsonl(
        p.workspace("michael-hyatt") / "processed/av_attribution_results.jsonl",
        [
            {
                "candidate_id": c.candidate_id,
                "result": {"attribution": {"status": "accepted_model"}},
            }
        ],
    )
    assert inventory(p, "michael-hyatt")[0].attribution == "matched"


def test_third_party_recording_not_advertised_as_ready(tmp_path):
    from vc_trace_collector.inventory import ready_recording
    from vc_trace_collector.wizard_models import source_items

    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    item = source_items(p, "michael-hyatt")[0]
    item.candidate.material_role = "third_party"
    assert not ready_recording(item)


@pytest.mark.parametrize("record", [[], None, {"artifact_id": []}])
def test_malformed_cache_does_not_hide_inventory(tmp_path, record):
    from vc_trace_collector.inventory import inventory
    from vc_trace_collector.storage import write_json

    p, _ = prepared(tmp_path, media=True)
    write_json(
        p.workspace("michael-hyatt") / "state/av_transcripts/broken.json", record
    )
    assert inventory(p, "michael-hyatt")[0].transcription == "pending"
