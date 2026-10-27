"""Keyboard-first, read-only inventory browser; actions return to the controller."""

from prompt_toolkit.filters import has_focus
from prompt_toolkit.layout import HSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, RadioList, TextArea

from .dashboard_ui import display_text

FILTERS = (
    "All",
    "Ready to process",
    "Downloaded",
    "Download failed",
    "Processing failed",
    "Needs review",
    "Approved",
    "Rejected",
    "Transcribed",
    "Diarized",
)


class InventoryBrowser:
    def __init__(self, ui, rows, name, job_status=""):
        self.rows = rows
        self.filter_name = "All"
        self.visible = list(rows)
        self.search = TextArea(multiline=False, prompt="Search: ", height=1)
        self.table = RadioList(self.values(), select_on_focus=True)
        self.table.window.height = Dimension(min=3, preferred=15)
        self.search.buffer.on_text_changed += lambda _: self.refresh()
        body = HSplit(
            [
                Label(
                    f"{len(rows)} sources | {sum(r.item.downloaded for r in rows)} downloaded | {sum(r.ready for r in rows)} recordings ready to process"
                ),
                Label(
                    job_status
                    or "Saved source inventory · no automatic stage advancement"
                ),
                self.search,
                Label(
                    lambda: (
                        f"Filter: {self.filter_name} · {len(self.visible)} shown · f changes filter"
                    )
                ),
                Label(
                    "    Platform Approve  Download Tx   Dia  Voice     Result    Title"
                ),
                self.table,
                Label("Tx = transcription · Dia = diarization · done = saved output"),
                Label("Enter details  / search  f filter  p transcribe & diarize"),
                Label(
                    "d download  r review  v voice reference  a more  j jobs  Esc back"
                ),
            ]
        )
        self.app = ui.application(
            name + " · Sources",
            body,
            [],
            footer="  ↑↓ Sources  Enter Details  Esc Investors  Ctrl-R Refresh  ? Help",
        )
        self.app.layout.focus(self.table)

        @self.app.key_bindings.add("enter", filter=has_focus(self.table), eager=True)
        def details(event):
            if self.table.current_value:
                event.app.exit(result=("details", self.table.current_value))

        @self.app.key_bindings.add("enter", filter=has_focus(self.search), eager=True)
        def leave_search(event):
            event.app.layout.focus(self.table)

        @self.app.key_bindings.add("/", filter=has_focus(self.table), eager=True)
        def search(event):
            event.app.layout.focus(self.search)

        @self.app.key_bindings.add("f", filter=has_focus(self.table), eager=True)
        def filter_rows(event):
            self.filter_name = FILTERS[
                (FILTERS.index(self.filter_name) + 1) % len(FILTERS)
            ]
            self.refresh()

        @self.app.key_bindings.add("escape", eager=True)
        def back(event):
            if event.app.layout.has_focus(self.search):
                event.app.layout.focus(self.table)
            else:
                event.app.exit(result=None)

        for key, action in {
            "p": "process_media",
            "d": "download",
            "r": "review",
            "v": "reference",
            "a": "actions",
            "j": "jobs",
            "c-r": "refresh",
            "?": "help",
        }.items():
            self.app.key_bindings.add(key, filter=has_focus(self.table), eager=True)(
                self.action(action)
            )

    @staticmethod
    def action(name):
        def invoke(event):
            event.app.exit(result=(name, None))

        return invoke

    def values(self):
        short = {
            "complete": "done",
            "pending": "wait",
            "unknown": "?",
            "downloaded": "saved",
            "auto_approved": "auto",
            "operator-approved": "operator",
            "review_required": "review",
            "not processed": "pending",
            "succeeded": "success",
        }

        def cell(value, width):
            return short.get(value, value)[:width].ljust(width)

        platforms = {
            "web_article": "article",
            "web_profile": "profile",
            "linkedin_export": "linkedin",
            "rss_feed": "RSS",
        }

        return [
            (
                r.candidate_id,
                display_text(
                    f"{cell(platforms.get(r.item.candidate.source_type.value, r.item.candidate.source_type.value), 8)} {cell(r.approval, 8)} {cell(r.download, 8)} {cell(r.transcription, 4)} {cell(r.diarization, 4)} {cell(r.attribution, 9)} {cell(r.processing, 9)} {r.item.candidate.title or r.item.candidate.canonical_url}"
                )
                .replace("\n", " ")
                .replace("\t", " "),
            )
            for r in self.visible
        ] or [
            ("", "No matching sources. Press f to change filter, or / to edit search.")
        ]

    def refresh(self):
        query = self.search.text.casefold()

        def matches(row):
            return {
                "All": True,
                "Ready to process": row.ready,
                "Downloaded": row.item.downloaded,
                "Download failed": row.download == "failed",
                "Processing failed": row.processing == "failed",
                "Needs review": row.approval == "pending"
                or row.processing == "review_required"
                or row.attribution == "uncertain",
                "Approved": row.approval in {"approved", "auto_approved"},
                "Rejected": row.approval == "rejected",
                "Transcribed": row.transcription == "complete",
                "Diarized": row.diarization == "complete",
            }[self.filter_name]

        self.visible = [
            r
            for r in self.rows
            if matches(r)
            and query
            in (r.item.label + " " + r.item.candidate.canonical_url).casefold()
        ]
        self.table.values = self.values()
        self.table.current_value = self.table.values[0][0]
        self.table._selected_index = 0
        self.app.invalidate()

    def run(self):
        return self.app.run()
