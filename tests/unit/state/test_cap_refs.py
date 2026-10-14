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
    caps._fetch_remote_cap_files(
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


def test_alias_preparation_tasks_have_distinct_progress_identity(tmp_path, monkeypatch):
    from toolang.common.layout import AgentLayout
    from toolang.state.prepare import prepare_agent_state

    layout = AgentLayout.resident(tmp_path, "aliases")
    layout.home.mkdir(parents=True)
    ref = "github://acme/caps/prompts/shared.md@main"
    layout.config.write_text(
        f'[prompts]\none = {{ref="{ref}"}}\ntwo = {{ref="{ref}"}}\n'
    )
    monkeypatch.setattr(
        caps, "_fetch_github_file", lambda *_: b"---\ndescription: Shared\n---\nBody.\n"
    )
    events = []
    prepare_agent_state(layout, progress=events.append)
    fetches = [
        e
        for e in events
        if e.subject == "cap" and e.stage == "fetch" and e.status == "running"
    ]
    assert len(fetches) == 2
    assert len({(e.scope, e.id) for e in fetches}) == 2


def test_source_change_retry_remains_visible_without_double_counting_caps(
    tmp_path, monkeypatch
):
    from io import StringIO
    from toolang.cli.common.progress import StartupProgress
    from toolang.common.layout import AgentLayout
    from toolang.state.prepare import prepare_agent_state

    layout = AgentLayout.resident(tmp_path, "retry")
    layout.home.mkdir(parents=True)
    layout.config.write_text(
        '[prompts]\none = {ref="github://acme/caps/prompts/shared.md@main"}\n'
    )
    fetches = []

    def fetch(ref):
        fetches.append(ref)
        if len(fetches) == 1:
            layout.program.write_text("# Changed while fetching\n")
        return b"---\ndescription: Shared\n---\nBody.\n"

    monkeypatch.setattr(caps, "_fetch_github_file", fetch)
    stream = StringIO()
    with StartupProgress(stream=stream) as progress:
        prepare_agent_state(layout, progress=progress)
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
    assert len(fetches) == 2
    assert stream.getvalue().count(" fetching") == 2
    assert stream.getvalue().count("Prepared 1 cap in ") == 1
