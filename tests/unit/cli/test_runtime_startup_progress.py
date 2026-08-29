from __future__ import annotations

from io import StringIO
from pathlib import Path

from toolang.base.types.progress import ProgressEvent, ProgressStage, ProgressStatus
from toolang.cli.common.startup_progress import (
    RuntimeStartupProgress,
    runtime_startup_failure_message,
)


def _event(
    activity: str,
    label: str,
    *,
    status: ProgressStatus = "running",
    detail: str | None = None,
) -> ProgressEvent:
    stage: ProgressStage = "start" if activity in {"server", "ready"} else "create"
    return ProgressEvent(
        id="runtime:launch-1",
        kind="runtime",
        stage=stage,
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

    progress(_event("server", "Starting server"))
    progress.finish()

    assert progress.current_stage == "Starting server"
    assert stream.getvalue() == ""


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


def test_tty_startup_clears_before_attaching_workload_output() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker:python:3.13-slim",
        stream=StringIO(),
        live=True,
    )

    progress(_event("launch", "Starting workload"))
    assert progress._display is not None

    progress(
        _event(
            "launch",
            "Starting workload",
            status="ok",
            detail="176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb",
        )
    )

    assert progress._display is None
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
            "validate",
            "Checking Toolang compatibility",
            status="failed",
            detail="x" * 200,
        )
    )

    assert progress.current_stage == "Checking Toolang compatibility"
    assert progress.failure_stage == "runtime.create"
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


def test_startup_failure_without_detail_keeps_its_structured_stage() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )

    progress(
        _event(
            "install",
            "Installing Toolang",
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

    assert progress.current_stage == "Installing Toolang"
    assert progress.failure_stage == "runtime.create"
    assert progress.failure_reason is None


def test_startup_install_failure_uses_the_resolved_package_source() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )
    progress(
        _event(
            "install",
            "Installing Toolang",
            status="failed",
            detail="installer exited",
        )
    )

    package_index = runtime_startup_failure_message(
        "alice",
        "docker",
        progress,
        RuntimeError("startup failed"),
    )
    wheel = runtime_startup_failure_message(
        "alice",
        "docker",
        progress,
        RuntimeError("startup failed"),
        dev_artifact=Path("dist/toolang-0.3.0-py3-none-any.whl"),
    )

    assert "Reason: Could not install Toolang from the package index." in package_index
    assert "Fix: Check the log and network access" in package_index
    assert (
        "Reason: Could not install Toolang from toolang-0.3.0-py3-none-any.whl."
        in wheel
    )
    assert "Fix: Rebuild the wheel and check the installation log." in wheel


def test_startup_compatibility_failure_uses_development_source_guidance() -> None:
    progress = RuntimeStartupProgress(
        "alice",
        "docker",
        stream=StringIO(),
        live=False,
    )
    progress(
        _event(
            "validate",
            "Checking Toolang compatibility",
            status="failed",
            detail="compatibility check failed",
        )
    )

    message = runtime_startup_failure_message(
        "alice",
        "docker",
        progress,
        RuntimeError("startup failed"),
        development_build=True,
    )

    assert "Reason: The Toolang package installed in the guest cannot start" in message
    assert "Fix: Build the current source with `uv build --wheel`" in message
    assert "again with `--dev dist`." in message
