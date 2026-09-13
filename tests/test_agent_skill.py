from pathlib import Path


def test_project_agent_skill_teaches_audited_staged_workflow() -> None:
    path = Path(".agents/skills/vc-trace-collector/SKILL.md")

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "vc-trace-collector doctor" in text
    assert "vc-trace-collector search-source" in text
    assert "vc-trace-collector review" in text
    assert "vc-trace-collector fetch-source" in text
    assert "vc-trace-collector review-voice" in text
    assert "vc-trace-collector process-source" in text
    assert "Do not use platform captions" in text
    assert "Do not bypass identity or source review" in text
    assert "HF_TOKEN=" not in text
    assert "cookie=" not in text.casefold()
