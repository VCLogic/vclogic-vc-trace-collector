import importlib
import importlib.util

import pytest


@pytest.mark.filterwarnings("ignore:torchaudio.*deprecated:UserWarning")
def test_pyannote_imports_with_av_local_dependencies() -> None:
    if importlib.util.find_spec("pyannote.audio") is None:
        pytest.skip("av-local extra is not installed")

    importlib.import_module("pyannote.audio")
