# Guided Collector v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stage-first wizard and uncommitted dashboard with one linear `wizard` run: identity → platforms → discovery → triage → fetch → voice references → process → export.

**Architecture:** `guided.py` is a thin sequential controller over existing `Pipeline` methods, persisting completed steps in `guided_state.json`. `triage.py` holds a pure `TriageState` plus a small full-screen prompt_toolkit view. `voice.py` holds pure speaker/interval helpers plus the voice-candidate lookup moved from `wizard_reference.py`.

**Tech Stack:** Python 3, typer, questionary (`TerminalPrompts`), rich progress, prompt_toolkit, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-guided-collector-v2-design.md`. Deviations, chosen to cut code (spec updated in Task 1):
- Resume uses a `guided_state.json` marker instead of inferring state from many files. A workspace without the marker but with confirmed identity resumes after the identity step.
- Platform step asks which platforms to **skip** (nothing ticked = search all) so `Prompts.check` needs no "checked by default" support.
- Identity "No" stops with a hint; the profile choice already happens before discovery in `resolve_profile`.
- `add_sources` creates candidates directly from the URL (human-vouched, approved) without fetching; collection fetches them later.

**Conventions:** run tests with `uv run pytest`. Commit after each task. End every commit message with:

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

---

## File map

| File | Action | Responsibility |
|---|---|---|
| `src/vc_trace_collector/guided.py` | create | Linear flow, resume marker, step methods |
| `src/vc_trace_collector/triage.py` | create | `TriageState` (pure) + `run_triage` (prompt_toolkit) |
| `src/vc_trace_collector/voice.py` | create | Suggestions, speaker grouping, range parsing, clip playback, voice-candidate lookup, AV dependency check, local reference staging |
| `src/vc_trace_collector/pipeline.py` | modify | add `add_sources` after `_save_plan` (~line 570) |
| `src/vc_trace_collector/cli.py` | modify | `wizard` command (lines 56–87), `stage_reference` import (line 97) |
| `src/vc_trace_collector/terminal.py` | modify | drop `pick_sources` |
| `src/vc_trace_collector/wizard_models.py` | modify | drop `pick_sources` from `Prompts` |
| `wizard.py`, `wizard_reference.py`, `dashboard*.py`, `inventory*.py` | delete | replaced |
| `tests/test_triage.py`, `tests/test_voice.py`, `tests/test_guided.py` | create | |
| `tests/test_wizard.py`, `tests/test_dashboard*.py`, `tests/test_inventory*.py` | delete | after moving kept tests |
| `README.md`, `.agents/skills/vc-trace-collector/SKILL.md` | modify | drop `--stage` and dashboard docs |

---

### Task 0: Snapshot uncommitted work (needs user OK)

`dashboard*.py`, `inventory*.py` and their tests are untracked. Deleting them later would be unrecoverable, so commit everything first.

- [ ] **Step 1: Confirm with the user** that the current uncommitted work may be committed as one snapshot on branch `guided-collector-v2`.
- [ ] **Step 2: Commit**

```bash
git add -A src tests docs .agents CHANGELOG.md README.md pyproject.toml uv.lock
git commit -m "wip: snapshot uncommitted dashboard and pipeline work before guided v2"
```

Leave `av-decisions.json`, `decisions.json`, `written-decisions.json`, `example.env` untracked.

---

### Task 1: Update spec for deviations

**Files:** Modify `docs/superpowers/specs/2026-10-27-guided-collector-v2-design.md`

- [ ] **Step 1:** Replace the "### Resume" table with: "Completed steps are recorded in `<workspace>/guided_state.json` as `{"completed": [...]}`. Without that file, a workspace with confirmed identity resumes after `identity`."
- [ ] **Step 2:** In Flow step 3, replace "Checkbox list ... all ticked by default" with "Checkbox list of platforms to skip; nothing ticked searches all."
- [ ] **Step 3:** In Flow step 2, replace the "No:" sentence with "No: stop and suggest re-running with the correct profile URL."
- [ ] **Step 4:** Replace the `add_sources` paragraph with: "New pipeline method `add_sources(slug, urls, reviewer)` appends human-vouched, approved candidates (type from URL, confidence 1.0) without fetching; canonical duplicates are ignored."
- [ ] **Step 5: Commit** `git commit -am "docs: align guided v2 spec with simpler resume and platform skip"`

---

### Task 2: `Pipeline.add_sources`

**Files:** Modify `src/vc_trace_collector/pipeline.py` (after `_save_plan`, ~line 570). Test: `tests/test_guided.py` (create).

- [ ] **Step 1: Write the failing test** — create `tests/test_guided.py` with the shared fakes (moved from `tests/test_wizard.py`) and the test:

```python
from pathlib import Path
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
        ["https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc#t=1"],
        reviewer="human",
    )
    assert len(added) == 1
    assert added[0].approval_status == ApprovalStatus.APPROVED
    assert added[0].source_type == "youtube"
    assert p.add_sources("michael-hyatt", [added[0].url], reviewer="human") == []
    urls = [c.canonical_url for c in p._load_plan("michael-hyatt").candidates]
    assert urls.count(added[0].canonical_url) == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/test_guided.py -v` — Expected: FAIL, `AttributeError: 'Pipeline' object has no attribute 'add_sources'`.

- [ ] **Step 3: Implement** — add after `_save_plan` in `pipeline.py` (import `_source_type`, `stable_id` from `.discovery` and `canonicalize_url` from `.policy` if not already imported there):

```python
    def add_sources(
        self, investor_slug: str, urls: list[str], *, reviewer: str
    ) -> list[SourceCandidate]:
        """Append human-supplied URLs as approved candidates; collection fetches them."""
        plan = self._load_plan(investor_slug)
        known = {candidate.canonical_url for candidate in plan.candidates}
        confidence = Confidence(score=1, method="human_added_in_triage", version="1")
        added = []
        for url in urls:
            canonical = canonicalize_url(url.strip())
            if not canonical.startswith(("https://", "http://")):
                raise ValueError(f"Not a public HTTP(S) URL: {url}")
            if canonical in known:
                continue
            known.add(canonical)
            added.append(
                SourceCandidate(
                    candidate_id=stable_id("candidate", canonical),
                    url=url.strip(),
                    canonical_url=canonical,
                    source_type=_source_type(canonical),
                    discovered_via="user_supplied_source",
                    discovery_queries=["human added in triage"],
                    identity_confidence=confidence,
                    source_confidence=confidence,
                    approval_status=ApprovalStatus.APPROVED,
                    reviewed_by=reviewer,
                    decision_at=utc_now(),
                    decision_reason="Added by human during triage",
                )
            )
        plan.candidates.extend(added)
        self._save_plan(investor_slug, plan)
        return added
```

If `canonicalize_url` keeps the `#t=1` fragment, adjust the test's second URL to a form it does collapse (check `policy.canonicalize_url`); the test's purpose is canonical dedup.

- [ ] **Step 4: Run** `uv run pytest tests/test_guided.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: add_sources appends human-supplied candidates"`

---

### Task 3: `voice.py` pure helpers

**Files:** Create `src/vc_trace_collector/voice.py`, `tests/test_voice.py`.

- [ ] **Step 1: Write failing tests** `tests/test_voice.py`:

```python
import pytest

from vc_trace_collector.av import DiarizedTurn
from vc_trace_collector.voice import parse_range, reference_interval, speakers


def turn(start, end, label):
    return DiarizedTurn(start_seconds=start, end_seconds=end, speaker_label=label)


def test_speakers_sorted_by_talk_time_with_longest_turn():
    rows = speakers(
        [turn(0, 10, "A"), turn(10, 70, "B"), turn(70, 75, "A"), turn(75, 80, "B")]
    )
    assert [(label, total) for label, total, _ in rows] == [("B", 65), ("A", 15)]
    assert rows[0][2].start_seconds == 10


def test_reference_interval_caps_at_thirty_seconds():
    assert reference_interval(turn(10, 70, "B")) == (10, 40)
    assert reference_interval(turn(5, 12, "B")) == (5, 12)


@pytest.mark.parametrize(
    "text,expected",
    [("03:10-03:40", (190, 220)), ("1:00:00 - 1:00:30", (3600, 3630)), ("5-9.5", (5, 9.5))],
)
def test_parse_range(text, expected):
    assert parse_range(text) == expected


@pytest.mark.parametrize("text", ["", "03:40-03:10", "abc-def", "10"])
def test_parse_range_rejects_bad_input(text):
    with pytest.raises(ValueError):
        parse_range(text)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_voice.py -v` — Expected: FAIL, `ModuleNotFoundError: vc_trace_collector.voice`.

- [ ] **Step 3: Implement** `src/vc_trace_collector/voice.py`:

```python
"""Voice-reference helpers: suggestions, speaker grouping, clips, candidate lookup."""

import math
import re
from collections import defaultdict

from .av import DiarizedTurn

MAX_REFERENCE_SECONDS = 30.0


def speakers(turns: list[DiarizedTurn]) -> list[tuple[str, float, DiarizedTurn]]:
    """(label, total seconds, longest turn), most talkative first."""
    total: dict[str, float] = defaultdict(float)
    longest: dict[str, DiarizedTurn] = {}
    for turn in turns:
        length = turn.end_seconds - turn.start_seconds
        total[turn.speaker_label] += length
        best = longest.get(turn.speaker_label)
        if best is None or length > best.end_seconds - best.start_seconds:
            longest[turn.speaker_label] = turn
    return sorted(
        ((label, total[label], longest[label]) for label in total),
        key=lambda row: -row[1],
    )


def reference_interval(turn: DiarizedTurn) -> tuple[float, float]:
    return turn.start_seconds, min(
        turn.end_seconds, turn.start_seconds + MAX_REFERENCE_SECONDS
    )


def _seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def parse_range(text: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*([\d:.]+)\s*-\s*([\d:.]+)\s*", text)
    if not match:
        raise ValueError("Use a range like 03:10-03:40")
    start, end = _seconds(match[1]), _seconds(match[2])
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError("Start must be before end")
    return start, end
```

- [ ] **Step 4: Run** `uv run pytest tests/test_voice.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: speaker grouping and range parsing for voice references"`

---

### Task 4: Move reference helpers into `voice.py`

**Files:** Modify `src/vc_trace_collector/voice.py`, `src/vc_trace_collector/cli.py:97`, `tests/test_voice.py`.

- [ ] **Step 1: Write failing tests** — append to `tests/test_voice.py` (moved from `test_wizard.py`, plus one new):

```python
from unittest.mock import Mock

from test_guided import prepared
from typer.testing import CliRunner

from vc_trace_collector.cli import create_app
from vc_trace_collector.storage import read_jsonl
from vc_trace_collector.voice import voice_candidate
from vc_trace_collector.wizard_models import source_items


def test_stage_reference_cli_records_plan_without_download(tmp_path):
    p, _ = prepared(tmp_path)
    local = tmp_path / "sample.wav"
    local.write_bytes(b"sample")
    p.fetch_source = Mock()
    result = CliRunner().invoke(
        create_app(lambda output: p),
        ["stage-reference", "--investor", "michael-hyatt", "--file", str(local),
         "--reviewer", "human:test"],
    )
    assert result.exit_code == 0, result.output
    assert "candidate:" in result.output
    p.fetch_source.assert_not_called()


def test_supplied_video_has_reference_backlink_after_download(tmp_path):
    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    voice = read_jsonl(
        p.workspace("michael-hyatt") / "identity/reference_voice_candidates.jsonl"
    )[0]
    assert voice["artifact_id"]


def test_voice_candidate_returns_id_and_local_media(tmp_path):
    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    item = next(i for i in source_items(p, "michael-hyatt") if i.candidate.candidate_id == c.candidate_id)
    cid, media = voice_candidate(p, "michael-hyatt", item, reviewer="human")
    assert cid.startswith("voice")
    assert media.is_file() and media.is_relative_to(p.workspace("michael-hyatt").resolve())
```

- [ ] **Step 2: Run** `uv run pytest tests/test_voice.py -v` — Expected: FAIL on `voice_candidate` import.

- [ ] **Step 3: Implement.** In `voice.py`:
  1. Move `MEDIA_SUFFIXES`, `missing_av_dependencies` and `stage_local_reference` **verbatim** from `wizard_reference.py`, with their imports.
  2. Move `reference_suggestions` **verbatim** from `dashboard_state.py` (define `APPROVED = {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}` in `voice.py`).
  3. Add:

```python
import shutil
import subprocess
from pathlib import Path

from .av import extract_audio_segment
from .collectors import is_acquired_media
from .models import MaterialRole, SourceDecision
from .process import load_artifact_records
from .storage import read_jsonl, write_jsonl

SPOKEN = {MaterialRole.SPOKEN_BY_TARGET, MaterialRole.REFERENCE_VOICE}


def voice_candidate(pipeline, slug, item, *, reviewer) -> tuple[str, Path]:
    """Voice-candidate id and local media path for a downloaded source."""
    workspace = pipeline.workspace(slug).resolve()
    cid = item.candidate.candidate_id
    if item.candidate.material_role not in SPOKEN:
        # review() creates the voice-candidate row for spoken sources.
        pipeline.review(
            slug,
            decisions=[
                SourceDecision(
                    candidate_id=cid,
                    status=item.candidate.approval_status,
                    reason="Chosen as voice reference recording",
                    decided_by=reviewer,
                    material_role=MaterialRole.SPOKEN_BY_TARGET,
                )
            ],
            reviewer=reviewer,
            confirm_identity=True,
        )
    artifact = next(
        (
            a
            for a in load_artifact_records(workspace)
            if a.original_metadata.get("candidate_id") == cid
            and is_acquired_media(a)
            and a.collection_method != "human_selected_reference_segment"
        ),
        None,
    )
    media = (workspace / artifact.relative_path).resolve() if artifact else None
    if media is None or not media.is_relative_to(workspace) or not media.is_file():
        raise ValueError("No downloaded media for this source")
    path = workspace / "identity/reference_voice_candidates.jsonl"
    rows = read_jsonl(path)
    row = next(r for r in rows if r["source_candidate_id"] == cid)
    if not row.get("artifact_id"):
        row["artifact_id"] = artifact.artifact_id
        write_jsonl(path, rows)
    return row["candidate_id"], media


def play(media: Path, start: float, end: float, workdir: Path) -> str | None:
    """Play a clip with ffplay; return the clip path when ffplay is unavailable."""
    clip = workdir / f"clip-{start:.0f}-{end:.0f}.wav"
    extract_audio_segment(media, clip, start_seconds=start, end_seconds=end)
    if not shutil.which("ffplay"):
        return str(clip)
    subprocess.run(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(clip)],
        check=False,
    )
    return None
```

  4. In `cli.py` line 97 change `from .wizard_reference import stage_local_reference` to `from .voice import stage_local_reference`.

- [ ] **Step 4: Run** `uv run pytest tests/test_voice.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "refactor: move reference helpers into voice module"`

---

### Task 5: `TriageState`

**Files:** Create `src/vc_trace_collector/triage.py`, `tests/test_triage.py`.

- [ ] **Step 1: Write failing tests** `tests/test_triage.py`:

```python
from vc_trace_collector.models import (
    ApprovalStatus, Confidence, MaterialRole, SourceCandidate, SourceType,
)
from vc_trace_collector.triage import TriageState


def cand(cid, score, source_type=SourceType.YOUTUBE, title=None):
    conf = Confidence(score=score, method="t", version="1")
    return SourceCandidate(
        candidate_id=cid, url=f"https://x.test/{cid}", canonical_url=f"https://x.test/{cid}",
        source_type=source_type, title=title or cid, discovery_queries=["q"],
        identity_confidence=conf, source_confidence=conf,
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
        "c": ApprovalStatus.REJECTED, "b": ApprovalStatus.APPROVED, "a": ApprovalStatus.PENDING,
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
    s = TriageState([cand("a", 0.9, title="Seed rounds"), cand("b", 0.8, title="Panel")])
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
```

- [ ] **Step 2: Run** `uv run pytest tests/test_triage.py -v` — Expected: FAIL, module missing.

- [ ] **Step 3: Implement** `src/vc_trace_collector/triage.py` (state part):

```python
"""Source triage: pure decision state plus a full-screen keyboard view."""

from collections import Counter

from .models import ApprovalStatus, MaterialRole, SourceDecision

ROLES = list(MaterialRole)
APPROVED = {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}


class TriageState:
    def __init__(self, candidates, threshold=0.8):
        self.rows = []
        self.threshold = threshold
        self.statuses: dict[str, ApprovalStatus] = {}
        self.roles: dict[str, MaterialRole] = {}
        self.skipped: set[str] = set()
        self.query = ""
        self.cursor = 0
        self.add(candidates)

    def add(self, candidates):
        self.rows = sorted(
            [*self.rows, *candidates],
            key=lambda c: (-c.identity_confidence.score, c.candidate_id),
        )

    def status(self, c):
        if c.source_type in self.skipped:
            return ApprovalStatus.REJECTED
        return self.statuses.get(c.candidate_id, c.approval_status)

    def role(self, c):
        return self.roles.get(c.candidate_id, c.material_role)

    @property
    def visible(self):
        q = self.query.casefold()
        return [
            c
            for c in self.rows
            if c.source_type not in self.skipped
            and q in f"{c.title or ''} {c.canonical_url}".casefold()
        ]

    def current(self):
        rows = self.visible
        return rows[min(self.cursor, len(rows) - 1)] if rows else None

    def move(self, delta):
        self.cursor = max(0, min(self.cursor + delta, len(self.visible) - 1))

    def mark(self, status):
        if c := self.current():
            self.statuses[c.candidate_id] = status

    def cycle_role(self):
        if c := self.current():
            self.roles[c.candidate_id] = ROLES[(ROLES.index(self.role(c)) + 1) % len(ROLES)]

    def approve_confident(self):
        for c in self.visible:
            if (
                self.status(c) == ApprovalStatus.PENDING
                and c.identity_confidence.score >= self.threshold
            ):
                self.statuses[c.candidate_id] = ApprovalStatus.APPROVED

    def toggle_platform(self, platform):
        self.skipped ^= {platform}
        self.cursor = 0

    def counts(self):
        return Counter(
            "approved" if s in APPROVED else s.value
            for s in map(self.status, self.rows)
        )

    def decisions(self, reviewer):
        out = []
        for c in self.rows:
            status, role = self.status(c), self.roles.get(c.candidate_id)
            if status == ApprovalStatus.PENDING:
                continue
            if status == c.approval_status and role in (None, c.material_role):
                continue
            out.append(
                SourceDecision(
                    candidate_id=c.candidate_id,
                    status=status,
                    reason="Platform skipped in triage"
                    if c.source_type in self.skipped
                    else "Human triage",
                    decided_by=reviewer,
                    material_role=role,
                )
            )
        return out
```

- [ ] **Step 4: Run** `uv run pytest tests/test_triage.py -v` — Expected: PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: pure triage decision state"`

---

### Task 6: Triage full-screen view

**Files:** Modify `src/vc_trace_collector/triage.py`. No automated test (layout only); manual check in Task 10.

- [ ] **Step 1: Implement** — append to `triage.py`:

```python
from prompt_toolkit import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .terminal import clean

MARKS = {ApprovalStatus.APPROVED: "✓", ApprovalStatus.AUTO_APPROVED: "✓", ApprovalStatus.REJECTED: "✗"}
FOOTER = (
    " ↑↓ move  a approve  x reject  r role  A approve ≥0.8  u add URLs  "
    "p skip platform  / search  enter details  s save & continue  q save & quit"
)


def run_triage(state: TriageState, title: str) -> str:
    """Show the table; return save|quit|search|add|platform|details."""
    keys = KeyBindings()

    def header():
        c = state.counts()
        return (
            f" {clean(title)} — {len(state.rows)} candidates  (approved {c['approved']}"
            f" · rejected {c['rejected']} · pending {c['pending']})\n"
            f" Skipped platforms: {', '.join(sorted(state.skipped)) or 'none'}"
            f"   Search: {state.query or '-'}"
        )

    def body():
        lines = []
        for i, c in enumerate(state.visible):
            text = (
                f" {MARKS.get(state.status(c), '·')}  {c.identity_confidence.score:.2f}  "
                f"{c.source_type.value:<15} {state.role(c).value:<19} "
                f"{clean(c.title or c.canonical_url)[:90]}\n"
            )
            lines.append(("reverse" if i == state.cursor else "", text))
        return lines or [("", " No candidates match.\n")]

    for key, action in {
        "a": lambda: state.mark(ApprovalStatus.APPROVED),
        "x": lambda: state.mark(ApprovalStatus.REJECTED),
        "r": state.cycle_role,
        "A": state.approve_confident,
        "up": lambda: state.move(-1),
        "down": lambda: state.move(1),
        "pageup": lambda: state.move(-15),
        "pagedown": lambda: state.move(15),
    }.items():
        keys.add(key)(lambda event, action=action: action())
    for key, result in {
        "s": "save", "q": "quit", "/": "search", "u": "add", "p": "platform", "enter": "details",
    }.items():
        keys.add(key)(lambda event, result=result: event.app.exit(result=result))
    keys.add("c-c")(lambda event: event.app.exit(exception=KeyboardInterrupt))

    table = FormattedTextControl(body, get_cursor_position=lambda: Point(0, state.cursor))
    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(header), height=2),
                Window(height=1, char="─"),
                Window(table),
                Window(height=1, char="─"),
                Window(FormattedTextControl(FOOTER), height=1),
            ]
        )
    )
    return Application(layout=layout, key_bindings=keys, full_screen=True).run()
```

Move these imports to the top of the file with the others.

- [ ] **Step 2: Run** `uv run pytest tests/test_triage.py -v` — Expected: PASS (import still works).
- [ ] **Step 3: Commit** `git add -A && git commit -m "feat: full-screen triage table"`

---

### Task 7: `Guided` flow — identity, platforms, triage, fetch, process, finish

**Files:** Create `src/vc_trace_collector/guided.py`. Test: `tests/test_guided.py`.

- [ ] **Step 1: Write failing tests** — append to `tests/test_guided.py`:

```python
from vc_trace_collector.guided import STEPS, Guided
from vc_trace_collector.models import ApprovalStatus as AS


def approve_all(state, title):
    for _ in state.rows:
        state.mark(AS.APPROVED)
        state.move(1)
    return "save"


def skip_all_platforms():
    from vc_trace_collector.guided import PLATFORMS
    return [p.value for p in PLATFORMS]


def test_written_run_from_existing_workspace_completes_and_verifies(tmp_path):
    p, c = prepared(tmp_path, approve=False)
    ui = Prompts(skip=skip_all_platforms(), confirm_identity=True)
    Guided(p, ui, triage=approve_all).run(investor="michael-hyatt")
    state = read_json(p.workspace("michael-hyatt") / "guided_state.json")
    assert state["completed"] == list(STEPS)
    assert any("Verified" in str(m) for m in ui.messages)


def test_resume_skips_completed_steps(tmp_path):
    p, _ = prepared(tmp_path)
    from vc_trace_collector.storage import write_json
    write_json(p.workspace("michael-hyatt") / "guided_state.json",
               {"completed": ["identity", "platforms", "triage"]})
    p.search_source = Mock()
    triage = Mock()
    Guided(p, Prompts(resume=True), triage=triage).run(investor="michael-hyatt")
    p.search_source.assert_not_called()
    triage.assert_not_called()


def test_quit_in_triage_saves_and_stops(tmp_path):
    p, c = prepared(tmp_path, approve=False)
    p.fetch_source = Mock()

    def quit_after_reject(state, title):
        state.mark(AS.REJECTED)
        return "quit"

    Guided(p, Prompts(skip=skip_all_platforms(), confirm_identity=True), triage=quit_after_reject).run(
        investor="michael-hyatt"
    )
    assert p.fetch_source.call_count == 0
    statuses = {x.candidate_id: x.approval_status for x in p._load_plan("michael-hyatt").candidates}
    assert AS.REJECTED in statuses.values()
    assert "triage" not in read_json(p.workspace("michael-hyatt") / "guided_state.json")["completed"]


def test_failing_source_is_reported_and_run_continues(tmp_path):
    p, c = prepared(tmp_path)
    p.process = Mock(side_effect=RuntimeError("boom"))
    ui = Prompts(skip=skip_all_platforms())
    Guided(p, ui, triage=lambda s, t: "save").run(investor="michael-hyatt")
    assert any("boom" in str(m) for m in ui.messages)
    assert "finish" in read_json(p.workspace("michael-hyatt") / "guided_state.json")["completed"]


def test_wizard_cli_rejects_noninteractive_terminal_without_work(tmp_path):
    factory = Mock()
    result = CliRunner().invoke(create_app(factory), ["wizard", "--investor", "michael-hyatt"])
    assert result.exit_code == 2
    assert "interactive terminal" in result.output
    factory.assert_not_called()
```

Notes for the tests: `prepared(approve=False)` leaves identity unconfirmed, so those tests answer `confirm_identity=True`. In `test_failing_source...` identity is confirmed, so the run asks `resume`; the fake answers False and `jump` defaults to the first step. Verification may fail on the fixture corpus; if so, assert on the summary message instead of `"Verified"` — check what `pipeline.verify` returns for `prepared()` in `test_pipeline_cli.py`.

- [ ] **Step 2: Run** `uv run pytest tests/test_guided.py -v` — Expected: FAIL, `ModuleNotFoundError: vc_trace_collector.guided`.

- [ ] **Step 3: Implement** `src/vc_trace_collector/guided.py`:

```python
"""Linear guided collection: one run from name to verified export."""

import os
import re
import shutil
from collections import Counter

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from .audit import redact
from .config import RunConfig
from .discovery import slugify
from .models import ApprovalStatus, MaterialRole, SourceType
from .progress import with_download_progress, with_processing_progress
from .public_search import agent_reach_public_search_provider
from .storage import read_json, write_json
from .triage import APPROVED, TriageState, run_triage
from .voice import missing_av_dependencies
from .wizard_models import Option, backend_options, options, source_items

STEPS = ("identity", "platforms", "triage", "fetch", "voice", "process", "finish")
PLATFORMS = [t for t in SourceType if t != SourceType.SUPPLIED]


class Guided:
    def __init__(self, pipeline, ui, *, triage=run_triage, dependency_check=None):
        self.pipeline = pipeline
        self.ui = ui
        self.triage_view = triage
        self.dependency_check = dependency_check or missing_av_dependencies
        self.reviewer = os.environ.get("USER") or "human"
        self.failures: list[str] = []
        self.step = STEPS[0]

    # --- run / resume -------------------------------------------------

    def run(self, *, name=None, investor=None):
        if investor and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", investor):
            raise ValueError("Invalid investor slug")
        slug = investor or self.start(name)
        if slug is None:
            return
        done = self.completed(slug)
        remaining = [s for s in STEPS if s not in done]
        if done and remaining and not self.ui.confirm(
            "resume", f"Continue from {remaining[0]}?"
        ):
            first = self.ui.select("jump", "Start from", options(STEPS))
            remaining = list(STEPS[STEPS.index(first) :])
        for step in remaining:
            self.step = step
            if getattr(self, step)(slug) is False:
                return
            self.mark(slug, step)

    def start(self, name):
        name = (name or self.ui.text("name", "Investor name")).strip()
        if not name:
            raise ValueError("Investor name is required")
        slug = slugify(name)
        if (self.pipeline.workspace(slug) / "identity/resolved_identity.json").exists():
            return slug
        from .wizard_profiles import preserve_firm_hint, resolve_profile

        profile = resolve_profile(self.pipeline, self.ui, name)
        if profile is None:
            return None
        url, config = profile
        slug = self.pipeline.discover(
            name=name, known_profile_url=url, config=config
        ).identity.slug
        preserve_firm_hint(self.pipeline, slug, config.firm, url)
        return slug

    def state_path(self, slug):
        return self.pipeline.workspace(slug) / "guided_state.json"

    def completed(self, slug):
        path = self.state_path(slug)
        if path.exists():
            return read_json(path)["completed"]
        confirmed = self.pipeline._load_identity(slug).resolution_status == "confirmed"
        return ["identity"] if confirmed else []

    def mark(self, slug, step):
        done = self.completed(slug)
        write_json(self.state_path(slug), {"completed": [*done, step] if step not in done else done})

    def config(self, slug):
        return RunConfig.model_validate(
            read_json(self.pipeline.workspace(slug) / "config_snapshot.json")
        )

    def models(self, slug):
        config = self.config(slug)
        return {
            "transcription_model": config.transcription_model or "turbo",
            "diarization_model": config.diarization_model
            or "pyannote/speaker-diarization-3.1",
        }

    def items(self, slug, *, downloaded=None, media=None):
        return [
            i
            for i in source_items(self.pipeline, slug)
            if i.candidate.approval_status in APPROVED
            and (downloaded is None or i.downloaded == downloaded)
            and (media is None or i.media == media)
        ]

    # --- steps --------------------------------------------------------

    def identity(self, slug):
        identity = self.pipeline._load_identity(slug)
        self.ui.show(identity.model_dump(mode="json"))
        if identity.resolution_status == "confirmed":
            return True
        if not self.ui.confirm("confirm_identity", f"Is this the right {identity.canonical_name}?"):
            self.ui.show("Stopped. Run wizard again with the correct profile URL.")
            return False
        self.pipeline.review(slug, decisions=[], reviewer=self.reviewer, confirm_identity=True)
        return True

    def platforms(self, slug):
        skip = set(
            self.ui.check(
                "skip",
                "Platforms to skip (nothing ticked = search all)",
                options(p.value for p in PLATFORMS),
            )
        )
        chosen = [p for p in PLATFORMS if p.value not in skip]
        if not chosen:
            return True
        config = self.config(slug)
        backend = "agent-reach" if shutil.which("mcporter") else "default"
        limit, ceiling = 10, max(config.maximum_search_operations, 20)
        self.ui.show(
            f"{len(chosen)} platforms · {limit} results/query · at most {ceiling} searches"
            f" · cost ceiling ${config.maximum_cost_usd} · backend {backend}"
        )
        if self.ui.confirm("edit_limits", "Edit limits?"):
            backend = self.ui.select("backend", "Search backend", backend_options())
            limit = self.integer("results", "Results per query (1–100)", limit, 1, 100)
            ceiling = self.integer("searches", "Search-operation ceiling", ceiling, ceiling, 10000)
        provider = agent_reach_public_search_provider() if backend == "agent-reach" else None
        found = 0
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
            TextColumn("{task.completed}/{task.total} platforms · {task.fields[found]} found"),
            console=Console(stderr=True),
        ) as bar:
            task = bar.add_task("Searching", total=len(chosen), found=0)
            for platform in chosen:
                bar.update(task, description=f"Searching {platform.value}")
                result = self.pipeline.search_source(
                    slug, platform, search_provider=provider,
                    limit_per_query=limit, maximum_search_operations=ceiling,
                )
                found += result.added
                bar.update(task, advance=1, found=found)
                self.failures += [f"search {platform.value}: {e}" for e in result.errors]
                if result.stopped_early:
                    self.ui.show(f"Search stopped at {platform.value}: {'; '.join(result.errors) or 'budget reached'}")
                    break
        return True

    def integer(self, key, message, default, minimum, maximum):
        while True:
            try:
                value = int(self.ui.text(key, message, str(default)))
                if minimum <= value <= maximum:
                    return value
            except ValueError:
                pass
            self.ui.show(f"Enter a whole number between {minimum} and {maximum}.")

    def triage(self, slug):
        identity = self.pipeline._load_identity(slug)
        state = TriageState(self.pipeline._load_plan(slug).candidates)
        while True:
            action = self.triage_view(state, identity.canonical_name)
            if action == "search":
                state.query = self.ui.text("search", "Search title/URL (blank = all)", state.query)
            elif action == "add":
                urls = self.ui.text("urls", "URLs to add (space-separated)").split()
                state.add(self.pipeline.add_sources(slug, urls, reviewer=self.reviewer))
            elif action == "platform":
                present = sorted({c.source_type.value for c in state.rows})
                state.toggle_platform(self.ui.select("platform", "Skip/unskip platform", options(present)))
            elif action == "details" and (c := state.current()):
                self.ui.show(next(i.model_dump(mode="json") for i in source_items(self.pipeline, slug)
                                  if i.candidate.candidate_id == c.candidate_id))
            elif action in {"save", "quit"}:
                decisions = state.decisions(self.reviewer)
                if decisions:
                    self.pipeline.review(slug, decisions=decisions, reviewer=self.reviewer, confirm_identity=True)
                self.ui.show(f"Saved {len(decisions)} decisions.")
                return action == "save"

    @with_download_progress
    def _fetch(self, slug, ids):
        return self.pipeline.fetch_source(slug, candidate_ids=ids)

    def fetch(self, slug):
        pending = self.items(slug, downloaded=False)
        if not pending:
            self.ui.show("Nothing new to download.")
            return True
        result = self._fetch(slug, {i.candidate.candidate_id for i in pending})
        self.failures += [f"fetch {f}" for f in result.failures]
        self.ui.show(f"Downloaded {result.collected} · failed {result.failed} · skipped {result.skipped}")
        return True

    def voice(self, slug):
        return True  # Task 8

    @with_processing_progress
    def _process(self, slug, cid, models):
        return self.pipeline.process(slug, candidate_ids={cid}, **models)

    def process(self, slug):
        workspace = self.pipeline.workspace(slug)
        has_reference = any(
            p.status == "verified_human" for p in self.pipeline._load_reference_profiles(workspace)
        )
        models = self.models(slug)
        for item in self.items(slug, downloaded=True):
            if item.candidate.material_role == MaterialRole.REFERENCE_VOICE:
                continue
            if item.media and not has_reference:
                self.failures.append(f"skipped {item.candidate.canonical_url}: no voice reference")
                continue
            try:
                docs = self._process(slug, item.candidate.candidate_id, models if item.media else {})
                counts = Counter(str(d.inclusion_status) for d in docs
                                 if d.source_candidate_id == item.candidate.candidate_id)
                self.ui.show(f"{item.candidate.title or item.candidate.canonical_url}: {dict(counts)}")
            except Exception as error:
                self.failures.append(f"process {item.candidate.canonical_url}: {redact(str(error))}")
                self.ui.show(f"Failed {item.candidate.canonical_url}: {redact(str(error))}")
        return True

    def finish(self, slug):
        self.pipeline.export(slug)
        result = self.pipeline.verify(slug)
        items = source_items(self.pipeline, slug)
        self.ui.show(
            f"{'Verified' if result.passed else 'NOT verified'} · "
            f"approved {sum(i.candidate.approval_status in APPROVED for i in items)} · "
            f"downloaded {sum(i.downloaded for i in items)} · "
            f"processed {sum(i.processing_status != 'not processed' for i in items)}\n"
            f"Output: {self.pipeline.workspace(slug)}"
        )
        if not result.passed:
            self.ui.show(result.model_dump(mode="json"))
        for failure in self.failures:
            self.ui.show(f"  ✗ {failure}")
        return True
```

Run `uv run ruff format src/vc_trace_collector/guided.py` (or the repo's formatter) to wrap long lines.

- [ ] **Step 4: Run** `uv run pytest tests/test_guided.py -v` — Expected: PASS. If `test_wizard_cli_rejects_noninteractive...` fails, it is fixed in Task 9.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: linear guided collection flow"`

---

### Task 8: Voice reference loop

**Files:** Modify `src/vc_trace_collector/guided.py` (replace the `voice` stub). Test: `tests/test_guided.py`.

- [ ] **Step 1: Write failing tests** — append:

```python
from vc_trace_collector.av import DiarizationResult, DiarizedTurn


def media_ready(tmp_path):
    p, c = prepared(tmp_path, media=True)
    p.fetch_source("michael-hyatt", candidate_ids={c.candidate_id})
    p.media_probe = lambda path: 600.0
    p.approve_reference_voice = Mock()
    return p, c


def test_voice_path_a_approves_longest_turn_of_chosen_speaker(tmp_path, monkeypatch):
    p, c = media_ready(tmp_path)
    monkeypatch.setattr("vc_trace_collector.guided.extract_audio_segment", lambda *a, **k: None)
    diarizer = Mock()
    diarizer.diarize.return_value = DiarizationResult(
        model="m", speaker_embeddings={},
        turns=[DiarizedTurn(start_seconds=0, end_seconds=5, speaker_label="HOST"),
               DiarizedTurn(start_seconds=5, end_seconds=65, speaker_label="GUEST")],
    )
    p._av_providers = Mock(return_value=(None, diarizer))
    ui = Prompts(add_reference=True, reference=c.candidate_id, speaker="use:GUEST", confirm_voice=True)
    Guided(p, ui, dependency_check=lambda: []).voice("michael-hyatt")
    kwargs = p.approve_reference_voice.call_args.kwargs
    assert (kwargs["start_seconds"], kwargs["end_seconds"]) == (5, 35)


def test_voice_path_b_uses_typed_range(tmp_path, monkeypatch):
    p, c = media_ready(tmp_path)
    monkeypatch.setattr("vc_trace_collector.guided.extract_audio_segment", lambda *a, **k: None)
    monkeypatch.setattr("vc_trace_collector.guided.play", lambda *a: None)
    p._av_providers = Mock(side_effect=RuntimeError("no model"))
    ui = Prompts(add_reference=True, reference=c.candidate_id, range="01:00-01:20", confirm_voice=True)
    Guided(p, ui, dependency_check=lambda: []).voice("michael-hyatt")
    kwargs = p.approve_reference_voice.call_args.kwargs
    assert (kwargs["start_seconds"], kwargs["end_seconds"]) == (60, 80)


def test_voice_missing_dependencies_skips_without_models(tmp_path):
    p, _ = media_ready(tmp_path)
    p._av_providers = Mock()
    ui = Prompts()
    Guided(p, ui, dependency_check=lambda: ["ffmpeg"]).voice("michael-hyatt")
    p._av_providers.assert_not_called()
    assert any("ffmpeg" in str(m) for m in ui.messages)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_guided.py -k voice -v` — Expected: FAIL (stub approves nothing).

- [ ] **Step 3: Implement** — add imports to `guided.py`:

```python
import tempfile
from pathlib import Path

from .av import extract_audio_segment
from .voice import parse_range, play, reference_interval, reference_suggestions, speakers, stage_local_reference, voice_candidate
```

Replace `voice`:

```python
    def voice(self, slug):
        if not self.items(slug, downloaded=True, media=True):
            return True
        missing = self.dependency_check()
        if missing:
            self.ui.show(
                f"Missing audio/video dependencies: {', '.join(missing)}. Install with "
                "uv sync --extra youtube --extra av --extra av-local, plus FFmpeg; set HF_TOKEN "
                "in .env. Audio/video sources will be skipped."
            )
            return True
        workspace = self.pipeline.workspace(slug)
        name = self.pipeline._load_identity(slug).canonical_name
        while True:
            count = sum(p.status == "verified_human"
                        for p in self.pipeline._load_reference_profiles(workspace))
            question = (f"Add another reference? ({count} approved)" if count
                        else "Add a voice reference now? (needed to process audio/video)")
            if not self.ui.confirm("add_reference", question):
                return True
            item = self.pick_recording(slug, name)
            if item is not None:
                self.reference_from(slug, item, name)

    def pick_recording(self, slug, name):
        media = self.items(slug, downloaded=True, media=True)
        suggested = reference_suggestions(media, name)
        choice = self.ui.select(
            "reference", "Recording with the investor's voice",
            [*(Option(value=i.candidate.candidate_id,
                      label=f"{i.candidate.title or i.candidate.canonical_url} · identity {i.candidate.identity_confidence.score:.2f}")
               for i in suggested),
             Option(value="browse", label="Browse all downloaded recordings"),
             Option(value="local", label="Local file")],
        )
        if choice == "browse":
            choice = self.ui.select("reference_all", "All downloaded recordings",
                                    [Option(value=i.candidate.candidate_id, label=i.label) for i in media])
        if choice == "local":
            path = Path(self.ui.text("reference_path", "Path to local audio/video")).expanduser()
            choice = stage_local_reference(self.pipeline, slug, path, self.reviewer)
            self._fetch(slug, {choice})
        return next((i for i in source_items(self.pipeline, slug)
                     if i.candidate.candidate_id == choice and i.downloaded), None)

    def reference_from(self, slug, item, name):
        voice_id, media = voice_candidate(self.pipeline, slug, item, reviewer=self.reviewer)
        models = self.models(slug)
        with tempfile.TemporaryDirectory() as tmp:
            interval = self.choose_speaker(slug, media, Path(tmp), models, name)
            if interval is None:
                interval = self.typed_range(media, Path(tmp))
            if interval is None:
                return
            start, end = interval
            duration = self.pipeline.media_probe(media)
            if not duration or end > duration:
                self.ui.show("That range is past the end of the recording.")
                return
            if not self.ui.confirm("confirm_voice", f"Only {name} speaks in {start:.0f}s–{end:.0f}s. Save as reference?"):
                return
        self.pipeline.approve_reference_voice(
            slug, candidate_id=voice_id, reviewer=self.reviewer,
            start_seconds=start, end_seconds=end,
            diarization_model=models["diarization_model"], embedding_model="",
        )
        self.ui.show("Reference saved.")

    def choose_speaker(self, slug, media, tmp, models, name):
        """Path A: diarize, let the user listen, return the chosen speaker's interval."""
        try:
            config = self.config(slug).model_copy(update=models)
            diarizer = self.pipeline._av_providers(config, needs_transcript=False)[1]
            audio = tmp / "audio.wav"
            extract_audio_segment(media, audio)
            rows = speakers(diarizer.diarize(audio).turns)
        except Exception as error:
            self.ui.show(f"Speaker detection failed ({redact(str(error))}); enter a time range instead.")
            return None
        turns = {label: turn for label, _, turn in rows}
        choices = [Option(value=f"play:{label}", label=f"▶ {label} · {total / 60:.0f} min") for label, total, _ in rows]
        choices += [Option(value=f"use:{label}", label=f"{label} is {name}") for label, _, _ in rows]
        choices.append(Option(value="range", label="None of these / wrong split — type a time range"))
        while True:
            choice = self.ui.select("speaker", f"Which speaker is {name}?", choices)
            if choice == "range":
                return None
            action, label = choice.split(":", 1)
            start, end = reference_interval(turns[label])
            if action == "use":
                return start, end
            clip = play(media, start, min(end, start + 8), tmp)
            if clip:
                self.ui.show(f"ffplay not found; listen to {clip}")

    def typed_range(self, media, tmp):
        """Path B: the user types the interval."""
        while True:
            text = self.ui.text("range", "Time range where only the investor speaks (e.g. 03:10-03:40, blank = cancel)")
            if not text:
                return None
            try:
                start, end = parse_range(text)
            except ValueError as error:
                self.ui.show(str(error))
                continue
            clip = play(media, start, end, tmp)
            if clip:
                self.ui.show(f"ffplay not found; listen to {clip}")
            return start, end
```

Note: `voice_candidate` is called before diarization so a role change (not spoken → spoken) is saved even if the user cancels; that is harmless.

- [ ] **Step 4: Run** `uv run pytest tests/test_guided.py -v` — Expected: PASS. The fake `select` returns the answer for `speaker` every time, so `use:GUEST` ends path A immediately; path B's test gets `RuntimeError` from `_av_providers` and falls through to `range`.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: voice reference loop with speaker pick or typed range"`

---

### Task 9: Wire CLI, drop old wizard and dashboard

**Files:** Modify `cli.py:56-87`, `terminal.py`, `wizard_models.py`. Delete old modules/tests.

- [ ] **Step 1: Replace the `wizard` command** in `cli.py`:

```python
    @app.command()
    def wizard(
        name: str | None = typer.Option(None, help="Investor name for a new run"),
        investor: str | None = typer.Option(
            None, help="Existing investor workspace slug"
        ),
        output_dir: Path = typer.Option(Path("outputs")),
    ) -> None:
        """Guided run: identity, sources, triage, fetch, voice reference, process, export."""
        from .audit import redact
        from .guided import Guided
        from .terminal import Cancelled, TerminalPrompts

        guided = None
        try:
            ui = TerminalPrompts()
            guided = Guided(make_pipeline(output_dir), ui)
            guided.run(name=name, investor=investor)
        except (Cancelled, KeyboardInterrupt):
            step = guided.step if guided else "the start"
            typer.echo(f"Saved. Run wizard again to continue from {step}.")
            raise typer.Exit(130) from None
        except Exception as error:
            typer.echo(f"Wizard stopped: {redact(str(error))}", err=True)
            raise typer.Exit(2) from None
```

- [ ] **Step 2: Remove `pick_sources`** from `TerminalPrompts` in `terminal.py` (and the now-unused `SourceItem`/`options` imports) and from the `Prompts` protocol in `wizard_models.py`.

- [ ] **Step 3: Delete replaced code**

```bash
git rm src/vc_trace_collector/wizard.py src/vc_trace_collector/wizard_reference.py \
  src/vc_trace_collector/dashboard.py src/vc_trace_collector/dashboard_jobs.py \
  src/vc_trace_collector/dashboard_state.py src/vc_trace_collector/dashboard_ui.py \
  src/vc_trace_collector/inventory.py src/vc_trace_collector/inventory_ui.py \
  tests/test_wizard.py tests/test_dashboard.py tests/test_dashboard_e2e.py \
  tests/test_dashboard_jobs.py tests/test_dashboard_state.py \
  tests/test_inventory.py tests/test_inventory_ui.py
```

Move `test_process_cli_can_select_written_candidate_without_processing_others` from the old `tests/test_wizard.py` into `tests/test_pipeline_cli.py` before deleting it (import `prepared` from `test_guided`).

- [ ] **Step 4: Find leftovers** `grep -rn "wizard_reference\|dashboard\|inventory\|pick_sources\|from .wizard import\|--stage" src tests` — Expected: no hits. Fix any.

- [ ] **Step 5: Run full suite** `uv run pytest -q` — Expected: all pass. `tests/test_agent_skill.py` will pass once docs are updated in Task 10 if it fails on `--stage` text only.
- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: wizard runs guided flow; remove stage wizard and dashboard"`

---

### Task 10: Docs and manual check

**Files:** `README.md`, `.agents/skills/vc-trace-collector/SKILL.md`, `CHANGELOG.md`.

- [ ] **Step 1:** In `README.md`, replace the "Interactive investor dashboard" section (lines ~15–79) and the `wizard --stage ...` examples (lines ~151–280) with one section:

```markdown
## Guided run

    uv run --extra youtube --extra av --extra av-local vc-trace-collector wizard

One linear run: name → confirm identity → platforms to skip → discovery →
triage table → download → voice references → processing → export and verify.
Progress is saved after each step; run `wizard --investor <slug>` to continue.

Triage keys: `a` approve, `x` reject, `r` role, `A` approve all ≥0.8, `u` add
URLs, `p` skip a platform, `/` search, `enter` details, `s` save & continue,
`q` save & quit.

Voice references: pick one of the suggested recordings, listen to each detected
speaker and choose the investor, or type a range such as `03:10-03:40`. Add
several references from different recordings to cope with mic and noise
differences.
```

- [ ] **Step 2:** In `SKILL.md` line ~38 change `wizard --stage discover --name "Michael Hyatt"` to `wizard --name "Michael Hyatt"`; remove other `--stage` mentions.
- [ ] **Step 3:** Add a CHANGELOG entry: "wizard is now one linear guided run with a triage table and voice-reference speaker picker; `--stage` and the dashboard are removed."
- [ ] **Step 4: Run** `uv run pytest -q` — Expected: all pass.
- [ ] **Step 5: Manual check** in a real terminal: `uv run --extra youtube --extra av --extra av-local vc-trace-collector wizard --name "<test investor>"`; walk through triage keys, Ctrl-C mid-run and resume, one path-A and one path-B reference.
- [ ] **Step 6: Commit** `git add -A && git commit -m "docs: document guided run"`
