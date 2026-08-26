from __future__ import annotations

from io import StringIO

from toolang.base.types.progress import ProgressEvent, ProgressStatus
from toolang.cli.common.shutdown_progress import RuntimeShutdownProgress


def _event(
    phase: str,
    label: str,
    *,
    status: ProgressStatus = "running",
    detail: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        id=f"shutdown:alice:{phase}",
        phase=f"shutdown.{phase}",
        label=label,
        status=status,
        detail=detail,
    )


def test_non_tty_shutdown_prints_each_active_stage_once() -> None:
    stream = StringIO()
    progress = RuntimeShutdownProgress(
        "alice",
        "docker:python:3.13-slim",
        stream=stream,
        live=False,
    )

    progress(_event("stop", "Stopping workload", detail="docker:python:3.13-slim"))
    progress(_event("stop", "Stopping workload", detail="docker:python:3.13-slim"))
    progress(_event("stop", "Stopping workload", status="ok"))
    progress(
        _event(
            "release",
            "Releasing sandbox resources",
            detail="docker:python:3.13-slim",
        )
    )
    progress.finish()

    assert stream.getvalue().splitlines() == [
        "Stopping workload: docker:python:3.13-slim",
        "Releasing sandbox resources: docker:python:3.13-slim",
    ]


def test_tty_shutdown_tracks_stage_and_clears_transient_line() -> None:
    stream = StringIO()
    progress = RuntimeShutdownProgress(
        "alice",
        "docker:python:3.13-slim",
        stream=stream,
        live=True,
    )

    progress(_event("stop", "Stopping workload", detail="container-1"))
    output = progress._text().plain
    progress.finish()

    assert "Cleaning up temporary agent alice" in output
    assert "docker:python:3.13-slim" in output
    assert "Stopping workload" in output
    assert "container-1" in output
    assert progress.current_stage == "Stopping workload"
