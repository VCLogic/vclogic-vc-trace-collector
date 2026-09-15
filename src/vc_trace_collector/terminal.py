"""Keyboard-driven presentation only; no collection or model operations."""

import re
import sys

import questionary
from rich.console import Console

from .audit import redact
from .wizard_models import Option, SourceItem, options


class Cancelled(Exception):
    pass


def clean(value: str) -> str:
    # Remote titles/snippets must not emit terminal control sequences.
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(redact(value)))


class TerminalPrompts:
    def __init__(self):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError(
                "Wizard requires an interactive terminal. Use discover, review, "
                "fetch-source and process-source for scripted/agent runs."
            )
        self.console = Console()

    @staticmethod
    def answer(question):
        answer = question.ask()
        if answer is None:
            raise Cancelled()
        return answer

    def text(self, key, message, default=""):
        return self.answer(questionary.text(message, default=default)).strip()

    def confirm(self, key, message):
        return self.answer(questionary.confirm(message, default=False))

    def select(self, key, message, options):
        return self.answer(
            questionary.select(
                message,
                choices=[
                    questionary.Choice(clean(o.label), value=o.value) for o in options
                ],
                use_search_filter=True,
                use_jk_keys=False,
            )
        )

    def check(self, key, message, options):
        return self.answer(
            questionary.checkbox(
                message,
                choices=[
                    questionary.Choice(clean(o.label), value=o.value) for o in options
                ],
                use_search_filter=True,
                use_jk_keys=False,
            )
        )

    def show(self, message):
        if isinstance(message, str):
            self.console.print(clean(message), markup=False, highlight=False)
        else:
            self.console.print_json(data=redact(message))

    def pick_sources(self, items: list[SourceItem], purpose: str) -> list[str]:
        if not items:
            self.show(
                "No eligible sources. Discovery/review or download may still be needed."
            )
            return []
        selected: set[str] = set()
        platforms = sorted({item.candidate.source_type.value for item in items})
        statuses = sorted({item.candidate.approval_status.value for item in items})
        platform, status, query, descending = "all", "all", "", False
        while True:
            visible = [
                item
                for item in items
                if (platform == "all" or item.candidate.source_type == platform)
                and (status == "all" or item.candidate.approval_status == status)
                and query.casefold()
                in (item.label + " " + item.candidate.canonical_url).casefold()
            ]
            visible.sort(
                key=lambda item: (
                    item.candidate.identity_confidence.score,
                    item.candidate.candidate_id,
                ),
                reverse=descending,
            )
            self.show(
                f"{purpose}: {len(visible)} visible; {len(selected)} selected. "
                "Unchecked items are unchanged. Ctrl+C cancels before saving."
            )
            action = self.select(
                "source_action",
                "Source selection",
                options(
                    [
                        "select",
                        "details",
                        "filter_text",
                        "filter_platform",
                        "filter_status",
                        "reverse_confidence_sort",
                        "clear_selection",
                        "done",
                        "cancel",
                    ]
                ),
            )
            if action == "done":
                return sorted(selected)
            if action == "cancel":
                return []
            if action == "filter_text":
                query = self.text("filter", "Text in title or URL (blank = all)", query)
            elif action == "filter_platform":
                platform = self.select(
                    "platform", "Platform", options(["all", *platforms])
                )
            elif action == "filter_status":
                status = self.select(
                    "status", "Approval status", options(["all", *statuses])
                )
            elif action == "reverse_confidence_sort":
                descending = not descending
            elif action == "clear_selection":
                selected.clear()
            elif visible and action == "select":
                choices = [
                    questionary.Choice(
                        clean(item.label),
                        value=item.candidate.candidate_id,
                        checked=item.candidate.candidate_id in selected,
                    )
                    for item in visible
                ]
                chosen = self.answer(
                    questionary.checkbox(
                        "Space toggles; type to search; Enter keeps selection",
                        choices=choices,
                        use_search_filter=True,
                        use_jk_keys=False,
                    )
                )
                selected.difference_update(
                    item.candidate.candidate_id for item in visible
                )
                selected.update(chosen)
            elif visible and action == "details":
                cid = self.select(
                    "details",
                    "Inspect source",
                    [
                        Option(value=item.candidate.candidate_id, label=item.label)
                        for item in visible
                    ],
                )
                self.show(
                    next(
                        item.model_dump(mode="json")
                        for item in visible
                        if item.candidate.candidate_id == cid
                    )
                )
