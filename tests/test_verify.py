from pathlib import Path

from vc_trace_collector.export import export_workspace
from vc_trace_collector.verify import verify_workspace
from test_process_export import document


def test_exported_workspace_verifies(tmp_path) -> None:
    export_workspace(
        tmp_path,
        investor_slug="michael-hyatt",
        identity_id="identity:michael",
        documents=[document("blog", "public writing")],
        config_hash="config-hash",
        exclusion_rules_hash="rules-hash",
    )

    result = verify_workspace(tmp_path)
    assert result.passed is True
    assert result.errors == []


def test_verification_detects_tampered_corpus(tmp_path) -> None:
    export_workspace(
        tmp_path,
        investor_slug="michael-hyatt",
        identity_id="identity:michael",
        documents=[document("blog", "public writing")],
        config_hash="config-hash",
        exclusion_rules_hash="rules-hash",
    )
    with (tmp_path / "corpus/blog.jsonl").open("a") as handle:
        handle.write('{"tampered":true}\n')

    result = verify_workspace(tmp_path)
    assert result.passed is False
    assert any("hash" in error.casefold() for error in result.errors)
