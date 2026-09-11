from hashlib import sha256

from vc_trace_collector.storage import ArtifactStore, StateStore, read_jsonl, write_json


def test_artifacts_are_addressed_by_sha256(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes(
        b"public evidence",
        category="web",
        suffix=".html",
        source_url="https://example.test/evidence",
        mime_type="text/html",
    )

    assert artifact.record.sha256 == sha256(b"public evidence").hexdigest()
    assert artifact.path.read_bytes() == b"public evidence"
    assert artifact.metadata_path.exists()


def test_same_bytes_keep_distinct_immutable_provenance_records(tmp_path) -> None:
    store = ArtifactStore(tmp_path)

    discovered = store.put_bytes(
        b"same public page",
        category="web",
        suffix=".html",
        source_url="https://example.test/page",
        collection_method="identity_evidence_http",
    )
    collected = store.put_bytes(
        b"same public page",
        category="web",
        suffix=".html",
        source_url="https://example.test/page",
        collection_method="http",
        original_metadata={"candidate_id": "candidate:one"},
    )

    assert discovered.path == collected.path
    assert discovered.metadata_path != collected.metadata_path
    assert discovered.metadata_path.exists()
    assert collected.metadata_path.exists()


def test_atomic_json_uses_canonical_key_order(tmp_path) -> None:
    path = tmp_path / "record.json"
    write_json(path, {"z": 1, "a": 2})

    assert path.read_text() == '{\n  "a": 2,\n  "z": 1\n}\n'


def test_state_store_resumes_successful_operations(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    assert state.is_complete("fetch:one", "input-hash") is False
    state.finish_operation("fetch:one", "input-hash", "artifact-1")

    reopened = StateStore(tmp_path / "state.sqlite")
    assert reopened.is_complete("fetch:one", "input-hash") is True
    assert reopened.output_id("fetch:one", "input-hash") == "artifact-1"


def test_jsonl_reader_ignores_blank_lines(tmp_path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text('{"a": 1}\n\n{"a": 2}\n')
    assert read_jsonl(path) == [{"a": 1}, {"a": 2}]
