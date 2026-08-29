from __future__ import annotations

from io import StringIO

from toolang.base.types.progress import ProgressEvent, ProgressStatus
from toolang.cli.common.progress import CliProgress


def _cap_materialize_event(
    label: str,
    status: ProgressStatus,
) -> ProgressEvent:
    return ProgressEvent(
        id="cap:skill:briceyan/reviewer",
        kind="prepare",
        stage="materialize",
        label=label,
        status=status,
    )


def test_materialize_activity_replaces_completed_extract_activity() -> None:
    stream = StringIO()
    progress = CliProgress(stream=stream, live=False)

    progress(_cap_materialize_event("Extract skill", "running"))
    progress(_cap_materialize_event("Materialize skill", "running"))
    progress(_cap_materialize_event("Materialize skill", "ok"))
    progress.finish()

    assert "Prepared 1 caps" in stream.getvalue()
    assert "1 running" not in stream.getvalue()
