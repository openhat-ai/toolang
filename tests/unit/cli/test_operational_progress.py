from __future__ import annotations

from io import StringIO
from pathlib import Path
import time

import pytest

from toolang.base.types.progress import (
    ProgressEvent,
    ProgressKind,
    ProgressStage,
    ProgressStatus,
)
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.common.progress import (
    CliProgress,
    make_cli_progress,
    runtime_startup_failure_message,
)


class _TTYBuffer(StringIO):
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError


def _event(
    kind: ProgressKind,
    stage: ProgressStage,
    label: str,
    *,
    item_id: str = "opaque/item",
    status: ProgressStatus = "running",
    detail: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        id=item_id,
        kind=kind,
        stage=stage,
        label=label,
        status=status,
        detail=detail,
    )


def test_non_tty_uses_one_append_only_grammar_for_all_kinds() -> None:
    stream = StringIO()
    with CliProgress(stream=stream) as progress:
        progress(_event("prepare", "fetch", "Fetching skill browser..."))
        progress(
            _event(
                "prepare",
                "fetch",
                "Fetched skill browser",
                status="ok",
            )
        )
        progress(_event("setup", "load", "Loading setup...", item_id="setup"))
        progress(
            _event(
                "setup",
                "load",
                "Loaded setup",
                item_id="setup",
                status="ok",
            )
        )
        progress(
            _event(
                "runtime",
                "create",
                "Preparing Docker sandbox...",
                item_id="runtime",
            )
        )
        progress(
            _event(
                "runtime",
                "create",
                "Prepared Docker sandbox",
                item_id="runtime",
                status="ok",
            )
        )

    assert stream.getvalue().splitlines() == [
        "Fetching skill browser...",
        "Fetched skill browser",
        "Loading setup...",
        "Loaded setup",
        "Preparing Docker sandbox...",
        "Prepared Docker sandbox",
    ]


def test_non_tty_keeps_checkpoints_and_terminal_outcomes() -> None:
    stream = StringIO()
    with CliProgress(stream=stream) as progress:
        progress(_event("runtime", "create", "Preparing Docker sandbox..."))
        progress(
            _event(
                "runtime",
                "create",
                "Prepared Docker sandbox",
            )
        )
        progress(
            _event(
                "runtime",
                "create",
                "Installing Toolang from the package index...",
            )
        )
        progress(
            _event(
                "runtime",
                "create",
                "Installed Toolang from the package index",
            )
        )
        progress(_event("runtime", "create", "Created runtime", status="ok"))

    assert stream.getvalue().splitlines() == [
        "Preparing Docker sandbox...",
        "Prepared Docker sandbox",
        "Installing Toolang from the package index...",
        "Installed Toolang from the package index",
        "Created runtime",
    ]


def test_non_tty_deduplicates_exact_events_but_keeps_outcomes() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream)
    running = _event("setup", "load", "Loading setup...")

    progress(running)
    progress(running)
    progress(_event("setup", "load", "Loaded setup", status="ok"))
    progress.close()
    progress.close()

    assert stream.getvalue().splitlines() == ["Loading setup...", "Loaded setup"]


def test_non_tty_cleanup_segment_can_own_one_leading_gap() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream, leading_gap=True)

    progress(_event("runtime", "stop", "Stopping agent..."))
    progress(_event("runtime", "stop", "Stopped agent", status="ok"))

    assert stream.getvalue() == "\nStopping agent...\nStopped agent\n"


def test_pending_prepare_set_derives_counts_without_parsing_ids() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream)
    ids = ("one", "two:with:colons", "not-a-cap-prefix")
    for item_id in ids:
        progress(
            _event(
                "prepare",
                "fetch",
                "Fetching skill...",
                item_id=item_id,
                status="pending",
            )
        )

    progress(
        _event(
            "prepare",
            "fetch",
            "Fetching skill browser...",
            item_id=ids[0],
        )
    )
    progress(
        _event(
            "prepare",
            "fetch",
            "Fetched skill browser",
            item_id=ids[0],
            status="ok",
        )
    )
    progress(
        _event(
            "prepare",
            "materialize",
            "Updating skill browser...",
            item_id=ids[0],
        )
    )
    progress(
        _event(
            "prepare",
            "materialize",
            "Updated skill browser",
            item_id=ids[0],
            status="ok",
        )
    )

    assert stream.getvalue().splitlines() == [
        "Fetching skill browser (0/3 caps)...",
        "Fetched skill browser (0/3 caps)",
        "Updating skill browser (0/3 caps)...",
        "Updated skill browser (1/3 caps)",
    ]


def test_new_prepare_set_resets_aggregate_counts() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream)
    for item_id in ("first", "second"):
        progress(
            _event(
                "prepare",
                "materialize",
                "Updating skill...",
                item_id=item_id,
                status="pending",
            )
        )
    for item_id in ("first", "second"):
        progress(
            _event(
                "prepare",
                "materialize",
                f"Updating skill {item_id}...",
                item_id=item_id,
            )
        )
        progress(
            _event(
                "prepare",
                "materialize",
                f"Updated skill {item_id}",
                item_id=item_id,
                status="ok",
            )
        )

    progress(
        _event(
            "prepare",
            "materialize",
            "Updating prompt...",
            item_id="third",
            status="pending",
        )
    )
    progress(
        _event(
            "prepare",
            "materialize",
            "Updating prompt third...",
            item_id="third",
        )
    )

    assert stream.getvalue().splitlines()[-1] == "Updating prompt third..."


def test_unstarted_cache_hit_is_silent() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream)

    progress(
        _event(
            "prepare",
            "materialize",
            "Skipped updating skill browser",
            status="skipped",
        )
    )

    assert stream.getvalue() == ""


def test_factory_exposes_optional_sink_without_changing_object_lifecycle() -> None:
    enabled = make_cli_progress(stream=StringIO())
    disabled = make_cli_progress(stream=StringIO(), enabled=False)

    assert enabled.sink is enabled
    assert disabled.sink is None
    with disabled:
        pass
    disabled.close()


def test_suspending_progress_never_swallows_the_body_error() -> None:
    progress = CliProgress(stream=StringIO())

    with pytest.raises(RuntimeError, match="warning failed"):
        with progress.suspended():
            raise RuntimeError("warning failed")


def test_setup_leaf_restores_still_running_runtime_activity() -> None:
    progress = CliProgress(stream=_TTYBuffer(), _reveal_seconds=60)
    progress(_event("runtime", "start", "Starting agent...", item_id="runtime"))
    progress(_event("setup", "load", "Loading setup...", item_id="setup"))

    assert progress._live_text().plain == "Loading setup..."

    progress(
        _event(
            "setup",
            "load",
            "Loaded setup",
            item_id="setup",
            status="ok",
        )
    )

    assert progress._live_text().plain == "Starting agent..."
    progress.close()


def test_tty_elapsed_is_per_activity_and_appears_after_one_second() -> None:
    now = [0.0]
    progress = CliProgress(
        stream=_TTYBuffer(),
        _clock=lambda: now[0],
        _reveal_seconds=60,
    )
    progress(_event("runtime", "create", "Installing Toolang..."))

    now[0] = 0.9
    assert progress._live_text().plain == "Installing Toolang..."
    now[0] = 1.2
    assert progress._live_text().plain == "Installing Toolang (1.2s)..."

    progress(
        _event(
            "runtime",
            "create",
            "Installed Toolang",
        )
    )
    assert progress._live_text().plain == "Installed Toolang (1.2s)"
    now[0] = 1.3
    progress(_event("runtime", "create", "Checking Toolang..."))
    assert progress._live_text().plain == "Checking Toolang..."
    progress.close()


def test_fast_tty_activity_cancels_delayed_reveal_before_segment_close() -> None:
    stream = _TTYBuffer()
    progress = CliProgress(stream=stream)

    progress(_event("setup", "load", "Loading setup..."))
    progress(_event("setup", "load", "Loaded setup", status="ok"))
    time.sleep(0.2)

    assert stream.getvalue() == ""
    assert progress._live_display is None
    progress.close()


def test_tty_stage_terminal_does_not_inherit_checkpoint_elapsed() -> None:
    now = [0.0]
    progress = CliProgress(
        stream=_TTYBuffer(),
        _clock=lambda: now[0],
        _reveal_seconds=60,
    )

    progress(_event("runtime", "create", "Checking Toolang..."))
    now[0] = 2.2
    progress(_event("runtime", "create", "Checked Toolang"))
    now[0] = 2.3
    progress(_event("runtime", "create", "Created runtime", status="ok"))

    assert progress._live_text().plain == "Created runtime"
    progress.close()


def test_tty_failure_cancels_a_pending_live_reveal() -> None:
    stream = _TTYBuffer()
    progress = CliProgress(stream=stream)
    progress(_event("runtime", "start", "Starting agent...", item_id="runtime"))

    progress(
        _event(
            "setup",
            "load",
            "Failed to load setup",
            item_id="setup",
            status="failed",
            detail="invalid config",
        )
    )
    time.sleep(0.2)

    assert stream.getvalue() == ""
    progress.close()


def test_tty_progress_has_no_spinner_or_status_glyph() -> None:
    progress = CliProgress(stream=_TTYBuffer(), _reveal_seconds=60)

    progress(_event("runtime", "start", "Waiting for the agent API..."))

    assert progress._live_text().plain == "Waiting for the agent API..."
    progress.close()


def test_progress_wraps_with_two_cell_hanging_indent() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream, max_width=20)

    progress(
        _event(
            "setup",
            "discover",
            "Discovering 模型 from a very long provider name...",
        )
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) > 1
    assert all(display_width(line) <= 20 for line in lines)
    assert all(line.startswith("  ") for line in lines[1:])


def test_first_failure_remains_authoritative() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream)
    progress(
        _event(
            "runtime",
            "create",
            "Installing Toolang...",
        )
    )
    progress(
        _event(
            "runtime",
            "create",
            "Failed to install Toolang",
            status="failed",
            detail="installer exited",
        )
    )
    progress(
        _event(
            "runtime",
            "start",
            "Failed to start agent",
            status="failed",
            detail="workload exited",
        )
    )
    progress(
        _event(
            "runtime",
            "create",
            "Installed Toolang",
            status="ok",
        )
    )

    assert progress.failure_stage == "runtime.create"
    assert progress.failure_label == "Failed to install Toolang"
    assert progress.failure_reason == "installer exited"
    assert stream.getvalue().splitlines() == ["Installing Toolang..."]


def test_runtime_failure_uses_one_stable_block() -> None:
    progress = CliProgress(stream=StringIO())
    progress(
        _event(
            "runtime",
            "create",
            "Failed to install Toolang",
            status="failed",
            detail="installer exited",
        )
    )

    message = runtime_startup_failure_message(
        progress,
        RuntimeError("startup failed"),
        log_path=Path("agent.log"),
    )

    assert message.splitlines() == [
        "Failed to install Toolang",
        "  Stage: runtime.create",
        "  Reason: Could not install Toolang from the package index",
        "  Fix: Check network access or use --dev PATH with a compatible wheel",
        "  Log: agent.log",
    ]


def test_setup_failure_uses_the_same_stable_block() -> None:
    progress = CliProgress(stream=StringIO())
    progress(
        _event(
            "setup",
            "discover",
            "Failed to discover models",
            status="failed",
            detail="catalog crashed",
        )
    )

    assert progress.failure_message(RuntimeError("refresh failed")).splitlines() == [
        "Failed to discover models",
        "  Stage: setup.discover",
        "  Reason: catalog crashed",
    ]
