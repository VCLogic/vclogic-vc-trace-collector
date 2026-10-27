"""Source triage: pure decision state plus a full-screen keyboard view."""

from collections import Counter

from prompt_toolkit import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .models import ApprovalStatus, MaterialRole, SourceDecision
from .terminal import clean

ROLES = list(MaterialRole)
APPROVED = {ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED}
MARKS = {
    ApprovalStatus.APPROVED: "✓",
    ApprovalStatus.AUTO_APPROVED: "✓",
    ApprovalStatus.REJECTED: "✗",
}
FOOTER = (
    " ↑↓ move  a approve  x reject  r role  A approve ≥0.8  u add URLs  "
    "p skip platform  / search  enter details  s save & continue  q save & quit"
)


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
            index = (ROLES.index(self.role(c)) + 1) % len(ROLES)
            self.roles[c.candidate_id] = ROLES[index]

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


def run_triage(state: TriageState, title: str) -> str:
    """Show the table; return save|quit|search|add|platform|details."""
    keys = KeyBindings()

    def header():
        c = state.counts()
        return (
            f" {clean(title)} — {len(state.rows)} candidates  (approved {c['approved']}"
            f" · rejected {c['rejected']} · pending {c['pending']})\n"
            f" Skipped platforms: {', '.join(sorted(state.skipped)) or 'none'}"
            f"   Search: {clean(state.query) or '-'}"
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
        "s": "save",
        "q": "quit",
        "/": "search",
        "u": "add",
        "p": "platform",
        "enter": "details",
    }.items():
        keys.add(key)(lambda event, result=result: event.app.exit(result=result))
    keys.add("c-c")(lambda event: event.app.exit(exception=KeyboardInterrupt))

    table = FormattedTextControl(
        body, get_cursor_position=lambda: Point(0, state.cursor)
    )
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
