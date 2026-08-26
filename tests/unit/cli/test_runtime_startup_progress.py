from __future__ import annotations

from io import StringIO

from toolang.base.types.progress import ProgressEvent, ProgressStatus
from toolang.cli.common.startup_progress import (
    RuntimeStartupProgress,
    runtime_startup_failure_message,
)


def _event(
    phase: str,
    label: str,
    *,
    status: ProgressStatus = "running",
    detail: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        id=f"startup:alice:{phase}",
        phase=f"startup.{phase}",
        label=label,
        status=status,
        detail=detail,
    )


def test_non_tty_startup_prints_each_active_stage_once() -> None:
    stream = StringIO()
    progress = RuntimeStartupProgress(
        "alice",
        "docker:python:3.13-slim",
        stream=stream,
        live=False,
    )

    progress(_event("prepare", "Preparing sandbox", detail="docker"))
    progress(_event("prepare", "Preparing sandbox", detail="docker"))
    progress(_event("prepare", "Preparing sandbox", status="ok"))
    progress(_event("launch", "Starting workload"))
    progress.finish()

    assert stream.getvalue().splitlines() == [
        "Preparing sandbox: docker",
        "Starting workload",
    ]


def test_tty_startup_tracks_stage_and_clears_transient_line() -> None:
    stream = StringIO()
    progress = RuntimeStartupProgress(
        "alice",
        "docker:python:3.13-slim",
        stream=stream,
        live=True,
    )

    progress(_event("install", "Installing Toolang", detail="package index"))
    output = progress._text().plain
    progress.finish()

    assert "Starting agent alice" in output
    assert "docker:python:3.13-slim" in output
    assert "Installing Toolang" in output
    assert "package index" in output
    assert progress.current_stage == "Installing Toolang"


def test_startup_failure_retains_full_reason_while_bounding_display() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )

    progress(
        _event(
            "validate",
            "Validating Toolang",
            status="failed",
            detail="x" * 200,
        )
    )

    assert progress.current_stage == "Validating Toolang"
    assert progress.failure_reason == "x" * 200
    displayed_detail = progress._text().plain.split(" · ")[-2]
    assert len(displayed_detail) == 80
    assert displayed_detail.endswith("…")


def test_controller_failure_replaces_a_completed_guest_stage() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )

    progress(_event("install", "Installing Toolang"))
    progress(_event("install", "Installing Toolang", status="ok"))
    progress(
        _event(
            "launch",
            "Starting workload",
            status="failed",
            detail="container exited",
        )
    )

    assert progress.current_stage == "Starting workload"
    assert progress.failure_reason == "container exited"


def test_startup_failure_uses_an_action_supported_by_the_calling_command() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )
    error = RuntimeError("installed package lacks required `too serve`")

    development = runtime_startup_failure_message(
        "alice",
        "docker",
        progress,
        error,
    )
    packaged = runtime_startup_failure_message(
        "alice",
        "docker",
        progress,
        error,
        development_hint=False,
    )

    assert "pass `--dev dist`" in development
    assert "Upgrade the Toolang package" in packaged
    assert "--dev" not in packaged
