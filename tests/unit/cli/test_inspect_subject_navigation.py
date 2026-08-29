"""Typed inspect subject navigation registry tests."""

from __future__ import annotations

import pytest

import toolang.cli.toolang.commands.thread as thread_commands


@pytest.mark.parametrize(
    "transition",
    thread_commands.INSPECT_SUBJECT_TRANSITIONS,
    ids=lambda transition: f"{transition.source}-{transition.name}",
)
def test_registered_subject_transition_drives_dispatch_and_allowed_values(
    transition: thread_commands._SubjectTransition,
) -> None:
    assert (
        thread_commands._subject_transition(transition.source, transition.name)
        is transition
    )
    assert transition.name in thread_commands._allowed_transitions(transition.source)


def test_subject_transition_registry_is_closed_and_unambiguous() -> None:
    registered = {
        (transition.source, transition.name)
        for transition in thread_commands.INSPECT_SUBJECT_TRANSITIONS
    }

    assert registered == {
        ("agent", "threads"),
        ("agent", "runs"),
        ("thread", "runs"),
        ("run", "steps"),
    }
    assert len(registered) == len(thread_commands.INSPECT_SUBJECT_TRANSITIONS)


@pytest.mark.parametrize(
    "projector",
    thread_commands.INSPECT_PROJECTORS,
    ids=lambda projector: f"{projector.source}-{projector.name}",
)
def test_registered_projector_drives_allowed_values(
    projector: thread_commands._ProjectorTransition,
) -> None:
    assert projector.name in thread_commands._allowed_projectors(projector.source)


def test_inspect_help_is_derived_from_registered_transitions() -> None:
    help_text = thread_commands._inspect_subject_help()

    for transition in thread_commands.INSPECT_SUBJECT_TRANSITIONS:
        assert transition.name in help_text
    for projector in thread_commands.INSPECT_PROJECTORS:
        assert projector.name in help_text
