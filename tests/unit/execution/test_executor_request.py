from dataclasses import fields

from toolang.base.types.message import Message
from toolang.execution.executor.request import RunRequest


def test_run_request_has_minimal_external_contract() -> None:
    assert tuple(field.name for field in fields(RunRequest)) == (
        "origin",
        "input",
        "run_id",
        "thread_id",
        "executable_kind",
        "executable_name",
        "model_selector",
        "request_id",
        "context",
    )


def test_run_request_defaults_are_independent() -> None:
    first = RunRequest(origin="chat")
    second = RunRequest(origin="chat")

    assert first.input == Message.user("")
    assert first.context == {}
    assert first.context is not second.context
