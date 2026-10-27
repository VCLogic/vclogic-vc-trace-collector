from vc_trace_collector.models import (
    ApprovalStatus,
    Confidence,
    SourceCandidate,
    SourceType,
)
from vc_trace_collector.triage import TriageState


def cand(cid, score, source_type=SourceType.YOUTUBE, title=None):
    conf = Confidence(score=score, method="t", version="1")
    return SourceCandidate(
        candidate_id=cid,
        url=f"https://x.test/{cid}",
        canonical_url=f"https://x.test/{cid}",
        source_type=source_type,
        title=title or cid,
        discovery_queries=["q"],
        identity_confidence=conf,
        source_confidence=conf,
    )


def test_rows_sorted_by_confidence_and_marking():
    s = TriageState([cand("a", 0.5), cand("b", 0.9)])
    assert [c.candidate_id for c in s.visible] == ["b", "a"]
    s.mark(ApprovalStatus.APPROVED)
    s.move(1)
    s.mark(ApprovalStatus.REJECTED)
    assert s.status(s.rows[0]) == ApprovalStatus.APPROVED
    assert s.status(s.rows[1]) == ApprovalStatus.REJECTED


def test_approve_confident_only_touches_pending_above_threshold():
    s = TriageState([cand("a", 0.79), cand("b", 0.8), cand("c", 0.95)])
    s.mark(ApprovalStatus.REJECTED)  # c, highest
    s.approve_confident()
    assert {c.candidate_id: s.status(c) for c in s.rows} == {
        "c": ApprovalStatus.REJECTED,
        "b": ApprovalStatus.APPROVED,
        "a": ApprovalStatus.PENDING,
    }


def test_skipped_platform_is_hidden_and_rejected():
    s = TriageState([cand("a", 0.9), cand("p", 0.9, SourceType.PODCAST)])
    s.toggle_platform("podcast")
    assert [c.candidate_id for c in s.visible] == ["a"]
    out = {d.candidate_id: d.status for d in s.decisions("human")}
    assert out == {"p": ApprovalStatus.REJECTED}
    s.toggle_platform("podcast")
    assert s.decisions("human") == []


def test_cycle_role_and_search():
    s = TriageState(
        [cand("a", 0.9, title="Seed rounds"), cand("b", 0.8, title="Panel")]
    )
    s.cycle_role()
    assert s.role(s.rows[0]) != s.rows[0].material_role
    s.query = "panel"
    assert [c.candidate_id for c in s.visible] == ["b"]
    s.mark(ApprovalStatus.APPROVED)
    decisions = {d.candidate_id: d for d in s.decisions("human")}
    assert decisions["b"].status == ApprovalStatus.APPROVED
    assert "a" not in decisions  # role change alone on a pending row is not saved


def test_add_rows():
    s = TriageState([cand("a", 0.5)])
    s.add([cand("n", 1.0)])
    assert s.visible[0].candidate_id == "n"


def test_run_triage_keys_drive_state():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from vc_trace_collector.triage import run_triage

    s = TriageState([cand("a", 0.5), cand("b", 0.9)])
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        pipe.send_text("a\x1b[Bxs")  # approve b, down, reject a, save
        assert run_triage(s, "Jane Doe") == "save"
    assert {c.candidate_id: s.status(c) for c in s.rows} == {
        "b": ApprovalStatus.APPROVED,
        "a": ApprovalStatus.REJECTED,
    }
