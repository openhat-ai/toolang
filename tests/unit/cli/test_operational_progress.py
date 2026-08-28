from __future__ import annotations

from io import StringIO
from pathlib import Path

from toolang.base.types.progress import ProgressEvent, ProgressStage, ProgressStatus
from toolang.cli.common.progress import CliProgress, runtime_startup_failure_message


def _event(
    stage: ProgressStage,
    label: str,
    *,
    status: ProgressStatus = "running",
    detail: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        id="runtime:launch-1",
        kind="runtime",
        stage=stage,
        label=label,
        status=status,
        detail=detail,
    )


def test_non_tty_progress_prints_materially_changed_activities_once() -> None:
    stream = StringIO()
    progress = CliProgress(
        agent="alice",
        sandbox="docker:python:3.13-slim",
        stream=stream,
        live=False,
    )

    progress(_event("create", "Preparing sandbox", detail="docker"))
    progress(_event("create", "Preparing sandbox", detail="docker"))
    progress(_event("create", "Preparing sandbox", status="ok"))
    progress(_event("start", "Waiting for agent API"))
    progress.finish()

    assert stream.getvalue().splitlines() == [
        "Preparing sandbox: docker",
        "Waiting for agent API",
    ]


def test_non_tty_prepare_progress_is_append_only() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream, live=False)
    event = ProgressEvent(
        id="agent:github://example/agent",
        kind="prepare",
        stage="fetch",
        label="Fetch agent",
        status="running",
        detail="github://example/agent",
    )

    progress(event)
    progress(event)
    progress(
        ProgressEvent(
            id=event.id,
            kind="prepare",
            stage="fetch",
            label="Fetch agent",
            status="ok",
        )
    )
    progress.finish(details=False)

    assert stream.getvalue().splitlines()[0] == ("Fetch agent: github://example/agent")


def test_progress_can_track_without_rendering() -> None:
    stream = StringIO()
    progress = CliProgress(
        agent="alice",
        sandbox="docker",
        stream=stream,
        live=False,
        enabled=False,
    )

    progress(_event("start", "Starting AgentServer"))
    progress.finish()

    assert progress.current_stage == "Starting AgentServer"
    assert stream.getvalue() == ""


def test_tty_progress_has_one_compact_operational_line() -> None:
    stream = StringIO()
    progress = CliProgress(
        agent="alice",
        sandbox="docker:python:3.13-slim",
        stream=stream,
        live=True,
    )
    event = _event("create", "Installing Toolang", detail="package index")

    progress(event)
    output = progress._operational_text(event).plain
    progress.finish()

    assert "agent alice" in output
    assert "docker:python:3.13-slim" in output
    assert "Installing Toolang" in output
    assert "package index" in output


def test_first_failure_remains_authoritative() -> None:
    progress = CliProgress(
        agent="alice",
        sandbox="docker",
        stream=StringIO(),
        live=False,
    )

    progress(
        _event(
            "create",
            "Installing Toolang",
            status="failed",
            detail="installer exited",
        )
    )
    progress(
        _event(
            "start",
            "Waiting for agent API",
            status="failed",
            detail="workload exited",
        )
    )

    assert progress.failure_stage == "runtime.create"
    assert progress.failure_label == "Installing Toolang"
    assert progress.failure_reason == "installer exited"


def test_install_failure_uses_the_resolved_package_source() -> None:
    progress = CliProgress(
        agent="alice",
        sandbox="docker",
        stream=StringIO(),
        live=False,
    )
    progress(
        _event(
            "create",
            "Installing Toolang",
            status="failed",
            detail="installer exited",
        )
    )

    package_index = runtime_startup_failure_message(
        "alice", "docker", progress, RuntimeError("startup failed")
    )
    wheel = runtime_startup_failure_message(
        "alice",
        "docker",
        progress,
        RuntimeError("startup failed"),
        dev_artifact=Path("dist/toolang-0.3.0-py3-none-any.whl"),
    )

    assert "Stage: runtime.create" in package_index
    assert "Activity: Installing Toolang" in package_index
    assert "Reason: Could not install Toolang from the package index." in package_index
    assert "Fix: Check the log and network access" in package_index
    assert (
        "Reason: Could not install Toolang from toolang-0.3.0-py3-none-any.whl."
        in wheel
    )


def test_cleanup_uses_the_same_presenter_contract() -> None:
    stream = StringIO()
    progress = CliProgress(
        agent="alice",
        sandbox="docker",
        stream=stream,
        live=False,
    )

    progress(_event("stop", "Stopping workload", detail="docker"))
    progress(_event("destroy", "Destroying runtime", detail="docker"))
    progress.finish()

    assert stream.getvalue().splitlines() == [
        "Stopping workload: docker",
        "Destroying runtime: docker",
    ]
