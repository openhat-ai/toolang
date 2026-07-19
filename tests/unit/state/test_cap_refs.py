"""Unit tests for remote cap reference resolution."""

from __future__ import annotations

import pytest

from toolang.state import caps


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
