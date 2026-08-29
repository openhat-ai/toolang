from __future__ import annotations

from io import StringIO
from pathlib import Path

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


def test_startup_progress_can_track_without_rendering() -> None:
    stream = StringIO()
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=stream,
        live=False,
        enabled=False,
    )

    progress(_event("launch", "Starting workload"))
    progress.finish()

    assert progress.current_stage == "Starting workload"
    assert stream.getvalue() == ""


def test_tty_startup_tracks_stage_and_clears_transient_line() -> None:
    stream = StringIO()
    progress = RuntimeStartupProgress(
        "alice",
        "docker:python:3.13-slim",
        stream=stream,
        live=True,
    )

    progress(_event("launch", "Starting workload", detail="docker"))
    output = progress._text().plain
    progress(_event("launch", "Starting workload", status="ok"))
    assert progress._display is None
    progress.finish()

    assert "Starting agent alice" in output
    assert "docker:python:3.13-slim" in output
    assert "Starting workload" in output
    assert progress.current_stage == "Starting workload"


def test_startup_failure_retains_full_reason_while_bounding_display() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )

    progress(
        _event(
            "ready",
            "Waiting for agent API",
            status="failed",
            detail="x" * 200,
        )
    )

    assert progress.current_stage == "Waiting for agent API"
    assert progress.failure_phase == "startup.ready"
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

    progress(_event("prepare", "Preparing sandbox"))
    progress(_event("prepare", "Preparing sandbox", status="ok"))
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


def test_startup_failure_without_detail_keeps_its_structured_phase() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )

    progress(
        _event(
            "launch",
            "Starting workload",
            status="failed",
        )
    )
    progress(
        _event(
            "ready",
            "Waiting for agent API",
            status="failed",
            detail="workload exited",
        )
    )

    assert progress.current_stage == "Starting workload"
    assert progress.failure_phase == "startup.launch"
    assert progress.failure_reason is None


def test_startup_failure_uses_the_guest_reason_and_diagnostic_log() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )
    progress(
        _event(
            "ready",
            "Waiting for agent API",
            status="failed",
            detail="agent server exited before becoming ready",
        )
    )

    message = runtime_startup_failure_message(
        "alice",
        "docker",
        progress,
        RuntimeError("startup failed"),
        log_path=Path("/tmp/agent.log"),
    )

    assert "Stage: Waiting for agent API" in message
    assert "Reason: agent server exited before becoming ready" in message
    assert "Log: /tmp/agent.log" in message
    assert "Fix:" not in message
