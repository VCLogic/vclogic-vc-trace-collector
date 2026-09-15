import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from vc_trace_collector.terminal import Cancelled, TerminalPrompts, clean
from vc_trace_collector.wizard_models import options


def test_checkbox_keyboard_selection_and_search(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        ui = TerminalPrompts()
        pipe.send_text("podcast \r")
        assert ui.check("platforms", "Sources", options(["youtube", "podcast"])) == [
            "podcast"
        ]


def test_ctrl_c_returns_cancellation():
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        pipe.send_bytes(b"\x03")
        try:
            TerminalPrompts.answer(questionary.text("Name"))
        except Cancelled:
            return
    raise AssertionError("Ctrl+C must cancel")


def test_terminal_text_cannot_emit_controls_or_tokens():
    result = clean("\x1b]52;c;clipboard\x07 hf_abcdefghijklmnop")
    assert "\x1b" not in result
    assert "\x07" not in result
    assert "hf_abcdefghijklmnop" not in result


def test_backend_choices_report_local_prerequisites(monkeypatch):
    from vc_trace_collector.wizard_models import backend_options

    monkeypatch.setattr("shutil.which", lambda name: None)
    labels = {item.value: item.label for item in backend_options()}
    assert "missing" in labels["agent-reach"]
    assert "DDG" in labels["default"]
