"""Full-screen terminal presentation using the toolkit already used by questionary."""

import asyncio
import json
import re
import shutil
import sys

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import (
    Button,
    CheckboxList,
    Dialog,
    Frame,
    Label,
    RadioList,
    TextArea,
)

from .audit import redact
from .terminal import Cancelled

STYLE = Style.from_dict(
    {
        "": "bg:#101820 #dae5eb",
        "dialog": "bg:#172630",
        "dialog frame.label": "#7ee0d2 bold",
        "dialog.body": "bg:#172630",
        "dialog shadow": "bg:#080f14",
        "button": "bg:#263e4c #dae5eb",
        "button.focused": "bg:#7ee0d2 #101820 bold",
        "radio-selected": "bg:#294552 #ffffff bold",
        "radio-checked": "#7ee0d2",
        "checkbox-selected": "bg:#294552 #ffffff bold",
        "checkbox-checked": "#7ee0d2",
        "header": "bg:#203743 #7ee0d2 bold",
        "footer": "bg:#203743 #dae5eb",
        "text-area": "bg:#101820 #ffffff",
    }
)


def display_text(value):
    text = (
        value
        if isinstance(value, str)
        else json.dumps(redact(value), indent=2, default=str, ensure_ascii=False)
    )
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(redact(text)))


class SourcePicker:
    """Single searchable selection table; details track the highlighted row."""

    def __init__(self, items):
        self.items = sorted(items, key=lambda i: i.candidate.identity_confidence.score)
        self.visible = list(self.items)
        self.saved = set()
        self.search = TextArea(multiline=False, prompt="Filter: ")
        self.table = CheckboxList(self.values())
        self.table.window.height = Dimension(min=2, preferred=8)
        self.search.buffer.on_text_changed += self.filter
        self.body = HSplit(
            [
                self.search,
                Label("  ID confidence | Platform     | Decision  | Title"),
                self.table,
                Frame(
                    Window(
                        FormattedTextControl(self.detail),
                        wrap_lines=True,
                        height=Dimension(min=3, preferred=8),
                    ),
                    title="Source evidence",
                ),
            ]
        )

    def values(self):
        return [
            (
                i.candidate.candidate_id,
                display_text(
                    f"{i.candidate.identity_confidence.score:.2f} | {i.candidate.source_type.value:12} | {i.candidate.approval_status.value:9} | {i.candidate.title or i.candidate.canonical_url}"
                ),
            )
            for i in self.visible
        ] or [("", "No matching sources")]

    def selected(self):
        self.saved.difference_update(i.candidate.candidate_id for i in self.visible)
        self.saved.update(v for v in self.table.current_values if v)
        return sorted(self.saved)

    def filter(self, _event):
        self.selected()
        query = self.search.text.casefold()
        self.visible = [
            i
            for i in self.items
            if query
            in (
                i.label
                + " "
                + i.candidate.canonical_url
                + " "
                + i.candidate.material_role.value
            ).casefold()
        ]
        self.table.values = self.values()
        self.table.current_values = [
            i.candidate.candidate_id
            for i in self.visible
            if i.candidate.candidate_id in self.saved
        ]
        # prompt_toolkit exposes no public setter for the highlighted row.
        self.table._selected_index = 0

    def detail(self):
        if not self.visible:
            return "No matching sources. Clear the filter to return."
        item = self.visible[min(self.table._selected_index, len(self.visible) - 1)]
        c = item.candidate
        evidence = "\n".join(
            str(e.get("excerpt") or e.get("claim", "")) for e in item.evidence
        )
        return display_text(
            f"{c.title or 'Untitled'}\n{c.canonical_url}\nRole: {c.material_role.value} | Identity: {c.identity_confidence.score:.2f} ({c.identity_confidence.method})\nDownload: {item.collection_status} | Processing: {item.processing_status}\n{evidence or 'No linked identity evidence; verify the person before approval.'}"
        )

    def select_visible(self):
        self.table.current_values = [i.candidate.candidate_id for i in self.visible]

    def clear(self):
        self.saved.clear()
        self.table.current_values = []


class DashboardPrompts:
    def __init__(self):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError(
                "Wizard requires an interactive terminal. Use scripted CLI commands for agent runs."
            )
        self.breadcrumb = "Investors"

    def application(self, title, body, buttons, *, cancel=True, footer=None):
        keys = KeyBindings()

        @keys.add("escape", eager=True)
        @keys.add("c-c", eager=True)
        def back(event):
            if cancel:
                event.app.exit(result=None)

        controls = [
            Button(label, handler=handler, width=max(12, len(label) + 4))
            for label, handler in buttons
        ]
        if (
            sum(max(12, len(label) + 4) + 1 for label, _ in buttons) + 6
            > shutil.get_terminal_size().columns
        ):
            body = HSplit([body, HSplit(controls)])
            controls = []
        dialog = Dialog(
            title=display_text(title),
            body=body,
            buttons=controls,
            with_background=True,
        )
        root = HSplit(
            [
                Window(
                    FormattedTextControl(
                        lambda: (
                            "  VC TRACE COLLECTOR   /   "
                            + display_text(self.breadcrumb)
                        )
                    ),
                    height=1,
                    style="class:header",
                ),
                dialog,
                Window(
                    FormattedTextControl(
                        footer
                        or "  ↑↓ Navigate   Space Select   Tab Buttons   Enter Confirm   Esc Back"
                    ),
                    height=1,
                    style="class:footer",
                ),
            ]
        )
        return Application(
            layout=Layout(root),
            key_bindings=keys,
            style=STYLE,
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.5,
        )

    def select(self, key, message, options):
        if not options:
            raise Cancelled()
        choices = RadioList(
            [(o.value, display_text(o.label)) for o in options], select_on_focus=True
        )
        app = self.application(
            message,
            choices,
            [
                ("Continue", lambda: get_app().exit(result=choices.current_value)),
                ("Back", lambda: get_app().exit(result=None)),
            ],
        )

        @app.key_bindings.add("enter", filter=has_focus(choices))
        def accept(event):
            event.app.exit(result=choices.current_value)

        result = app.run()
        if result is None:
            raise Cancelled()
        return result

    def check(self, key, message, options):
        if not options:
            return []
        choices = CheckboxList([(o.value, display_text(o.label)) for o in options])

        def all_items():
            choices.current_values = [o.value for o in options]

        app = self.application(
            message,
            choices,
            [
                ("Continue", lambda: get_app().exit(result=choices.current_values)),
                ("Select all", all_items),
                ("Clear", lambda: setattr(choices, "current_values", [])),
                ("Back", lambda: get_app().exit(result=None)),
            ],
        )

        @app.key_bindings.add("enter", filter=has_focus(choices))
        def accept(event):
            event.app.exit(result=choices.current_values)

        result = app.run()
        if result is None:
            raise Cancelled()
        return result

    def text(self, key, message, default=""):
        field = TextArea(text=default, multiline=False)
        app = self.application(
            message,
            field,
            [
                ("Continue", lambda: get_app().exit(result=field.text)),
                ("Back", lambda: get_app().exit(result=None)),
            ],
        )
        app.layout.focus(field)

        @app.key_bindings.add("enter", filter=has_focus(field))
        def accept(event):
            event.app.exit(result=field.text)

        result = app.run()
        if result is None:
            raise Cancelled()
        return result.strip()

    def confirm(self, key, message):
        return bool(
            self.application(
                "Confirm",
                TextArea(text=display_text(message), read_only=True, scrollbar=True),
                [
                    ("Not now", lambda: get_app().exit(result=False)),
                    ("Confirm", lambda: get_app().exit(result=True)),
                ],
            ).run()
        )

    def show(self, message):
        self.application(
            "Details · Esc to return",
            TextArea(
                text=display_text(message),
                read_only=True,
                scrollbar=True,
                width=Dimension(preferred=100),
            ),
            [
                ("Continue", lambda: get_app().exit(result=True)),
            ],
        ).run()

    def busy(self, message, function):
        """Keep identity lookups responsive; no new operations on cancel."""
        app = self.application(
            message,
            Label("Working… please wait."),
            [],
            cancel=False,
        )

        async def execute():
            try:
                result = await asyncio.to_thread(function)
                app.exit(result=result)
            except Exception as error:
                app.exit(exception=error)

        return app.run(pre_run=lambda: app.create_background_task(execute()))

    def pick_sources(self, items, purpose):
        if not items:
            self.show("No eligible sources.")
            return []
        picker = SourcePicker(items)
        while True:
            result = self.application(
                f"{purpose.title()} · {len(items)} sources · least confident first",
                picker.body,
                [
                    ("Continue", lambda: get_app().exit(result=picker.selected())),
                    ("Select visible", picker.select_visible),
                    ("Clear", picker.clear),
                    ("Details", lambda: get_app().exit(result="details")),
                    ("Back", lambda: get_app().exit(result=[])),
                ],
            ).run()
            if result != "details":
                return result or []
            if picker.visible:
                item = picker.visible[
                    min(picker.table._selected_index, len(picker.visible) - 1)
                ]
                self.show(item.model_dump(mode="json"))

    def watch(self, output_dir, job_id):
        from .dashboard_jobs import list_jobs, request_pause

        def content():
            job = next((j for j in list_jobs(output_dir) if j["id"] == job_id), {})
            total, completed = job.get("total", 0), job.get("completed", 0)
            ratio = min(1, completed / total) if total else 0
            bar = "█" * int(30 * ratio) + "░" * (30 - int(30 * ratio))
            stage_total = job.get("progress_total")
            stage_done = job.get("progress_completed", 0)
            progress = f"{stage_done}/{stage_total}" if stage_total else "Working…"
            return display_text(
                f"{job.get('slug', '')}  •  {job.get('stage', '')}\n\n"
                f"{job.get('status', 'unknown')}\n{bar}  {completed}/{total} sources\n"
                f"Failed: {job.get('failed', 0)}\n\n{job.get('detail', '')}\n{progress}\n\n"
                f"{job.get('error', '')}\n\nBack leaves the worker running. Reopen Jobs to reconnect."
            )

        while True:
            action = self.application(
                "Live progress",
                Window(FormattedTextControl(content), wrap_lines=True),
                [
                    ("Back", lambda: get_app().exit(result="back")),
                    ("Pause", lambda: get_app().exit(result="pause")),
                    ("Resume / retry", lambda: get_app().exit(result="resume")),
                    ("Outcomes", lambda: get_app().exit(result="outcomes")),
                ],
            ).run()
            if action in {None, "back"}:
                return
            if action == "outcomes":
                row = next((j for j in list_jobs(output_dir) if j["id"] == job_id), {})
                self.show(
                    row.get("outcomes", [])
                    or row.get("error")
                    or "No finished items yet."
                )
            if action == "pause" and self.confirm(
                "pause_job",
                "Pause after the current item finishes? The active item is not interrupted; completed results remain saved.",
            ):
                request_pause(output_dir, job_id)
            if action == "resume" and self.confirm(
                "resume_job",
                "Resume unfinished/failed items from this job? Successful items remain untouched. Existing budgets still apply.",
            ):
                from .dashboard_jobs import resume_job

                try:
                    job_id = resume_job(output_dir, job_id)["id"]
                except (ValueError, RuntimeError) as error:
                    self.show(str(error))
