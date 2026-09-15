import re
from pathlib import Path

from typer.main import get_command

from vc_trace_collector.cli import create_app


def test_project_agent_skill_teaches_audited_staged_workflow() -> None:
    path = Path(".agents/skills/vc-trace-collector/SKILL.md")

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    references = re.findall(r"\]\((references/[^)]+)\)", text)
    assert len(references) == 4
    for reference in references:
        text += (path.parent / reference).read_text(encoding="utf-8")
    # Test real links and command existence; behavioral scenarios are independently
    # exercised rather than asserting exact instruction wording.
    commands = get_command(create_app()).commands
    documented = set(re.findall(r"vc-trace-collector ([a-z-]+)", text))
    assert {
        "wizard",
        "review",
        "fetch-source",
        "process",
        "stage-reference",
    } <= documented
    assert documented <= commands.keys()
    assert "identity/reference_voice_candidates.jsonl" in text
    assert "HF_TOKEN=" not in text
    assert "cookie=" not in text.casefold()


def test_claude_wrapper_resolves_to_shared_skill():
    path = Path(".claude/skills/vc-trace-collector/SKILL.md")
    text = path.read_text()
    target = re.search(r"\]\(([^)]+SKILL.md)\)", text).group(1)
    assert (path.parent / target).resolve() == Path(
        ".agents/skills/vc-trace-collector/SKILL.md"
    ).resolve()
    assert (path.parent / target).is_file()
