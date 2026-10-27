from unittest.mock import Mock

from test_pipeline_cli import pipeline
from typer.testing import CliRunner

from vc_trace_collector.cli import create_app
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


def test_add_sources_appends_approved_and_skips_duplicates(tmp_path):
    p, _ = prepared(tmp_path)
    added = p.add_sources(
        "michael-hyatt",
        [
            "https://www.youtube.com/watch?v=abc",
            "https://www.youtube.com/watch?v=abc&utm_source=x",
        ],
        reviewer="human",
    )
    assert len(added) == 1
    assert added[0].approval_status == ApprovalStatus.APPROVED
    assert added[0].source_type == "youtube"
    assert p.add_sources("michael-hyatt", [added[0].url], reviewer="human") == []
    urls = [c.canonical_url for c in p._load_plan("michael-hyatt").candidates]
    assert urls.count(added[0].canonical_url) == 1
