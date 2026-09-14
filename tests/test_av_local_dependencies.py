import importlib
import importlib.util

import pytest

from vc_trace_collector.av import (
    PyannoteDiarizationProvider,
    PyannoteEmbeddingProvider,
)


@pytest.mark.filterwarnings("ignore:torchaudio.*deprecated:UserWarning")
def test_pyannote_imports_with_av_local_dependencies() -> None:
    if importlib.util.find_spec("pyannote.audio") is None:
        pytest.skip("av-local extra is not installed")

    importlib.import_module("pyannote.audio")

    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    with pytest.raises(LocalEntryNotFoundError):
        hf_hub_download(
            "vc-trace-collector/nonexistent",
            "config.yaml",
            use_auth_token=False,
            local_files_only=True,
        )


@pytest.mark.filterwarnings("ignore:torchaudio.*deprecated:UserWarning")
@pytest.mark.parametrize(
    "provider_class",
    [PyannoteDiarizationProvider, PyannoteEmbeddingProvider],
)
def test_pyannote_checkpoint_load_preserves_existing_safe_globals(
    monkeypatch, provider_class
) -> None:
    if importlib.util.find_spec("pyannote.audio") is None:
        pytest.skip("av-local extra is not installed")

    import pyannote.audio
    import torch
    from torch.torch_version import TorchVersion

    original = list(torch.serialization.get_safe_globals())
    torch.serialization.add_safe_globals([TorchVersion])
    before = set(torch.serialization.get_safe_globals())
    observed: set[str] = set()

    def fake_from_pretrained(*args, **kwargs):
        observed.update(
            f"{item.__module__}.{item.__name__}"
            for item in torch.serialization.get_safe_globals()
            if hasattr(item, "__module__") and hasattr(item, "__name__")
        )
        return object()

    class FakePipeline:
        from_pretrained = staticmethod(fake_from_pretrained)

    monkeypatch.setattr(pyannote.audio, "Pipeline", FakePipeline)
    monkeypatch.setattr(pyannote.audio, "Inference", fake_from_pretrained)

    try:
        provider_class("pyannote/test", token="test-token")

        assert {
            "torch.torch_version.TorchVersion",
            "pyannote.audio.core.task.Specifications",
            "pyannote.audio.core.task.Problem",
            "pyannote.audio.core.task.Resolution",
        }.issubset(observed)
        assert set(torch.serialization.get_safe_globals()) == before
    finally:
        torch.serialization.clear_safe_globals()
        torch.serialization.add_safe_globals(original)
