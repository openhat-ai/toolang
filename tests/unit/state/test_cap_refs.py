"""Unit tests for remote cap reference resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from toolang.base.types.progress import ProgressEvent
from toolang.state import state as caps


def test_remote_shorthand_falls_back_to_main_when_branch_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        caps,
        "_github_repo_default_branch",
        lambda _owner, _repo: (_ for _ in ()).throw(ValueError("rate limited")),
    )
    monkeypatch.setattr(
        caps,
        "_github_remote_exists",
        lambda kind, ref: (
            kind == "psyche"
            and ref == "github://briceyan/agents/psyches/senior-engineer.md@main"
        ),
    )

    assert caps.resolve_remote_ref("psyche", "briceyan/senior-engineer") == (
        "github://briceyan/agents/psyches/senior-engineer.md@main"
    )


def test_remote_cap_keeps_one_progress_id_across_prepare_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda *_args: "main")
    monkeypatch.setattr(caps, "_github_remote_exists", lambda *_args: True)
    monkeypatch.setattr(caps, "_fetch_github_file", lambda _ref: b"Review carefully\n")
    events: list[ProgressEvent] = []
    progress_id = "cap:psyche:briceyan/reviewer"

    canonical = caps.resolve_remote_ref(
        "psyche",
        "briceyan/reviewer",
        progress=events.append,
        progress_id=progress_id,
    )
    caps._remote_materialized_files(
        relative_entry_path=Path("referenced/psyches/reviewer.md"),
        kind="psyche",
        name="reviewer",
        ref=canonical,
        progress=events.append,
        progress_id=progress_id,
    )

    assert [event.stage for event in events] == [
        "resolve",
        "resolve",
        "fetch",
        "fetch",
    ]
    assert {event.id for event in events} == {progress_id}
