from __future__ import annotations

from pathlib import Path

from toolang.base.types.progress import ProgressEvent
from toolang.common.progress import LAUNCH_PROGRESS_FILE_ENV
from toolang.up.launch_progress import launch_progress_sink


def test_launch_progress_sink_writes_only_closed_setup_tokens(tmp_path: Path) -> None:
    path = tmp_path / "launch.events"
    path.write_text("", encoding="ascii")
    sink = launch_progress_sink({LAUNCH_PROGRESS_FILE_ENV: str(path)})
    assert sink is not None

    sink(
        ProgressEvent(
            id="setup:alice",
            kind="setup",
            stage="load",
            label="untrusted label",
            status="running",
            detail="untrusted detail",
        )
    )
    sink(
        ProgressEvent(
            id="runtime:launch",
            kind="runtime",
            stage="start",
            label="Starting",
            status="running",
        )
    )

    assert path.read_text(encoding="ascii") == "setup.load.running\n"


def test_launch_progress_sink_ignores_missing_and_symlink_files(
    tmp_path: Path,
) -> None:
    missing = launch_progress_sink(
        {LAUNCH_PROGRESS_FILE_ENV: str(tmp_path / "missing.events")}
    )
    assert missing is not None
    missing(
        ProgressEvent(
            id="setup:alice",
            kind="setup",
            stage="load",
            label="Loading",
            status="running",
        )
    )

    target = tmp_path / "target.events"
    target.write_text("", encoding="ascii")
    link = tmp_path / "link.events"
    link.symlink_to(target)
    linked = launch_progress_sink({LAUNCH_PROGRESS_FILE_ENV: str(link)})
    assert linked is not None
    linked(
        ProgressEvent(
            id="setup:alice",
            kind="setup",
            stage="load",
            label="Loading",
            status="running",
        )
    )

    assert target.read_text(encoding="ascii") == ""
