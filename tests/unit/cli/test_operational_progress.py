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
    assert progress._live_text().plain == "Installing Toolang (1s)..."

    progress(
        _event(
            "runtime",
            "create",
            "Installed Toolang",
        )
    )
    assert progress._live_text().plain == "Installed Toolang (1s)"
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


@pytest.mark.parametrize(
    ("seconds", "expected"), [(60, "1m0s"), (68, "1m8s"), (3661, "1h1m1s")]
)
def test_operational_progress_uses_compact_duration(
    seconds: int, expected: str
) -> None:
    now = [0.0]
    progress = CliProgress(
        stream=_TTYBuffer(), _clock=lambda: now[0], _reveal_seconds=60
    )
    try:
        progress(_event("runtime", "create", "Installing Toolang..."))
        now[0] = float(seconds)
        assert progress._live_text().plain == f"Installing Toolang ({expected})..."
        progress(_event("runtime", "create", "Installed Toolang"))
        assert progress._live_text().plain == f"Installed Toolang ({expected})"
    finally:
        progress.close()


def _startup_event(
    item_id: str,
    *,
    stage: ProgressStage = "fetch",
    status: ProgressStatus = "running",
    scope: str = "home",
    cached: bool = False,
    complete: bool = False,
) -> ProgressEvent:
    from dataclasses import replace

    return replace(
        _event("prepare", stage, "Working...", item_id=item_id, status=status),
        subject="cap",
        ref=f"github://example/caps/{item_id}",
        scope=scope,
        cached=cached,
        complete=complete,
    )


def _finish_startup(progress: CliProgress) -> None:
    progress(
        ProgressEvent(
            id="state",
            kind="prepare",
            stage="materialize",
            label="Prepared State",
            status="ok",
            subject="state",
            complete=True,
        )
    )


def test_startup_slots_reuse_rows_then_collapse_to_one_dim_summary(monkeypatch) -> None:
    from toolang.cli.common.progress import PreparationProgress

    monkeypatch.setenv("TERM", "xterm-256color")
    stream = _TTYBuffer()
    now = [0.0]
    with PreparationProgress(
        stream=stream, _clock=lambda: now[0], _reveal_seconds=3600
    ) as progress:
        progress(_startup_event("one"))
        progress(_startup_event("two"))
        assert progress._live_text().plain.splitlines() == [
            "github://example/caps/one fetching",
            "github://example/caps/two fetching",
        ]
        assert progress._live_text().style == "dim"
        progress(_startup_event("one", stage="fetch", status="ok"))
        assert len(progress._live_text().plain.splitlines()) == 2
        progress(_startup_event("one", stage="materialize"))
        progress(_startup_event("one", stage="materialize", status="ok", complete=True))
        progress(_startup_event("three"))
        assert progress._live_text().plain.splitlines() == [
            "github://example/caps/three fetching",
            "github://example/caps/two fetching",
        ]
        # A late event for a completed occupant cannot overwrite its replacement.
        progress(_startup_event("one"))
        assert "one fetching" not in progress._live_text().plain
        progress(_startup_event("two", stage="materialize", status="ok", complete=True))
        assert progress._live_text().plain == "github://example/caps/three fetching"
        progress(
            _startup_event("three", stage="materialize", status="ok", complete=True)
        )
        assert progress._live_text().plain == ""
        assert stream.getvalue() == ""
        now[0] = 2.0
        _finish_startup(progress)
        _finish_startup(progress)
    output = stream.getvalue()
    assert output.count("Prepared 3 caps in 2s") == 1
    assert "\x1b[2m" in output
    assert "Preparing caps" not in output


@pytest.mark.parametrize("terminal", [False, True])
def test_cached_startup_is_silent_even_after_reveal_threshold(terminal: bool) -> None:
    from toolang.cli.common.progress import PreparationProgress

    stream = _TTYBuffer() if terminal else StringIO()
    now = [0.0]
    with PreparationProgress(stream=stream, _clock=lambda: now[0]) as progress:
        progress(_startup_event("cached", cached=True))
        progress(
            _startup_event(
                "cached",
                stage="materialize",
                status="skipped",
                cached=True,
                complete=True,
            )
        )
        now[0] = 60.0
        _finish_startup(progress)
        assert progress._reveal_timer is None
        assert progress._live_display is None
    assert stream.getvalue() == ""


def test_startup_distinguishes_scopes_omits_cache_and_keeps_plain_output_bounded() -> (
    None
):
    from toolang.cli.common.progress import PreparationProgress

    stream = StringIO()
    with PreparationProgress(stream=stream) as progress:
        progress(_startup_event("same", scope="root"))
        progress(_startup_event("same", scope="home"))
        progress(_startup_event("cached", cached=True))
        progress(_startup_event("same", scope="root", stage="materialize"))
        progress(_startup_event("same", scope="root", stage="materialize"))
        progress(
            _startup_event(
                "same", scope="home", stage="materialize", status="ok", complete=True
            )
        )
        progress(
            _startup_event(
                "same", scope="root", stage="materialize", status="ok", complete=True
            )
        )
        _finish_startup(progress)
    lines = stream.getvalue().splitlines()
    assert lines[:3] == [
        "github://example/caps/same fetching",
        "github://example/caps/same fetching",
        "github://example/caps/same updating",
    ]
    assert len(lines) == 4
    assert lines[-1].startswith("Prepared 2 caps in ")
    assert "cached" not in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_startup_failure_and_interruption_clear_slots_without_success() -> None:
    from toolang.cli.common.progress import PreparationProgress

    stream = _TTYBuffer()
    with PreparationProgress(stream=stream, _reveal_seconds=3600) as progress:
        progress(_startup_event("one"))
        progress(_startup_event("one", status="failed", complete=True))
        _finish_startup(progress)
        assert progress.failure_stage == "prepare.fetch"
        assert not any(progress._slots)
    assert "Prepared" not in stream.getvalue()
    stream = _TTYBuffer()
    with pytest.raises(KeyboardInterrupt):
        with PreparationProgress(stream=stream, _reveal_seconds=3600) as progress:
            progress(_startup_event("one"))
            raise KeyboardInterrupt
    progress(_startup_event("late"))
    assert progress._live_display is None
    assert stream.getvalue() == ""


def test_startup_session_is_shared_across_nested_launch_boundaries() -> None:
    from dataclasses import replace
    from toolang.cli.common.progress import make_cli_progress

    stream = StringIO()
    with make_cli_progress(stream=stream, preparation=True) as outer:
        outer(replace(_startup_event("agent"), subject="agent", complete=False))
        outer(
            replace(
                _startup_event("agent", status="ok", complete=True), subject="agent"
            )
        )
        with make_cli_progress(preparation=True) as inner:
            assert inner is outer
            inner(_startup_event("dependency"))
            inner(
                _startup_event(
                    "dependency", stage="materialize", status="ok", complete=True
                )
            )
            _finish_startup(inner)
        assert not outer._closed
    assert stream.getvalue().count("Prepared agent and 1 cap in ") == 1


def test_startup_narrow_terminal_preserves_action_and_short_terminal_limits_rows(
    monkeypatch,
) -> None:
    import os
    from toolang.cli.common.progress import PreparationProgress

    stream = _TTYBuffer()
    monkeypatch.setattr(stream, "fileno", lambda: 1)
    monkeypatch.setattr(os, "get_terminal_size", lambda *_: os.terminal_size((24, 2)))
    with PreparationProgress(
        stream=stream, max_width=24, _reveal_seconds=3600
    ) as progress:
        progress(_startup_event("one"))
        progress(_startup_event("two"))
        text = progress._live_text().plain
        assert len(text.splitlines()) == 1
        assert text.endswith(" fetching")
        assert display_width(text) <= 24
        with progress.suspended():
            assert progress._live_display is None
        assert len(progress._live_text().plain.splitlines()) == 1


@pytest.mark.parametrize("ended", ["finished", "closed", "failed"])
def test_nested_launch_does_not_reuse_an_ended_startup_session(ended):
    from toolang.cli.common.progress import make_cli_progress

    with make_cli_progress(stream=StringIO(), preparation=True) as outer:
        if ended == "finished":
            _finish_startup(outer)
        elif ended == "closed":
            outer.close()
        else:
            outer(_startup_event("broken", status="failed", complete=True))
        with make_cli_progress(stream=StringIO(), preparation=True) as inner:
            assert inner is not outer


def test_suspended_startup_defers_worker_output_and_summary():
    from toolang.cli.common.progress import PreparationProgress

    stream = StringIO()
    with PreparationProgress(stream=stream) as progress:
        with progress.suspended():
            progress(_startup_event("one"))
            assert stream.getvalue() == ""
            progress(
                _startup_event("one", stage="materialize", status="ok", complete=True)
            )
            _finish_startup(progress)
            assert stream.getvalue() == ""
        assert stream.getvalue().startswith("Prepared 1 cap in ")


def test_rejected_cap_error_survives_a_successful_state_publication():
    from dataclasses import replace
    from toolang.cli.common.progress import PreparationProgress

    stream = StringIO()
    with PreparationProgress(stream=stream) as progress:
        progress(_startup_event("broken"))
        progress(
            replace(
                _startup_event("broken", status="failed", complete=True),
                label="Failed to fetch prompt broken",
                detail="Remote content was not found",
            )
        )
        _finish_startup(progress)
        _finish_startup(progress)
    output = stream.getvalue()
    assert output.count("Failed to fetch prompt broken") == 1
    assert "Remote content was not found" in output
    assert "Prepared" not in output


@pytest.mark.parametrize("terminal", [False, True])
def test_cap_failure_keeps_parallel_slots_and_replacements_visible(terminal):
    from dataclasses import replace
    from toolang.cli.common.progress import PreparationProgress

    stream = _TTYBuffer() if terminal else StringIO()
    with PreparationProgress(stream=stream, _reveal_seconds=3600) as progress:
        for item_id in ("one", "broken", "three", "four"):
            progress(_startup_event(item_id))
        progress(
            replace(
                _startup_event("broken", status="failed"),
                label="Failed to fetch prompt broken",
                detail="Connection closed",
            )
        )
        assert progress.failure_stage == "prepare.fetch"
        assert [item[1].id for item in progress._slots if item] == [
            "one",
            "three",
            "four",
        ]
        progress(_startup_event("five"))
        assert [item[1].id for item in progress._slots if item] == [
            "one",
            "five",
            "three",
            "four",
        ]
        progress(_startup_event("broken"))
        progress(_startup_event("one", stage="materialize"))
        if terminal:
            assert progress._live_text().plain.splitlines() == [
                "github://example/caps/one updating",
                "github://example/caps/five fetching",
                "github://example/caps/three fetching",
                "github://example/caps/four fetching",
            ]
        else:
            assert "github://example/caps/five fetching" in stream.getvalue()
            assert "github://example/caps/one updating" in stream.getvalue()
        for item_id in ("one", "five", "three", "four"):
            progress(
                _startup_event(item_id, stage="materialize", status="ok", complete=True)
            )
        assert not any(progress._slots)
        _finish_startup(progress)
        _finish_startup(progress)
    assert stream.getvalue().count("Failed to fetch prompt broken") == 1
    assert "Connection closed" in stream.getvalue()
    assert "Prepared" not in stream.getvalue()


def test_failed_state_clears_parallel_slots_after_a_cap_failure():
    from dataclasses import replace
    from toolang.cli.common.progress import PreparationProgress

    stream = _TTYBuffer()
    with PreparationProgress(stream=stream, _reveal_seconds=3600) as progress:
        progress(_startup_event("broken"))
        progress(_startup_event("still-running"))
        progress(_startup_event("broken", status="failed"))
        assert any(progress._slots)
        progress._reveal_live()
        assert progress._live_display is not None
        progress(
            replace(
                _startup_event("state", status="failed", complete=True),
                subject="state",
            )
        )
        assert not any(progress._slots)
        assert progress._live_display is None
        assert progress._slot_timer is None
    assert "Prepared" not in stream.getvalue()


def test_enabled_preparation_does_not_reuse_a_disabled_outer_session():
    stream = StringIO()
    with make_cli_progress(enabled=False, preparation=True) as outer:
        with make_cli_progress(stream=stream, preparation=True) as inner:
            assert inner is not outer
            inner(_startup_event("one"))
            inner(
                _startup_event("one", stage="materialize", status="ok", complete=True)
            )
    assert stream.getvalue().count("Prepared 1 cap in ") == 1
