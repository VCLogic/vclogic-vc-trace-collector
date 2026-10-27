"""Keyboard-driven presentation only; no collection or model operations."""

import re
import sys

import questionary
from rich.console import Console

from .audit import redact


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
