from __future__ import annotations

from toolang.catalog.chore import ChoreFile
import pytest

from toolang.lang.ast import Program, parse_chore_declarations
from toolang.lang.errors import ToolangValidationError


@pytest.mark.parametrize("parse", [Program.from_source, parse_chore_declarations])
def test_inline_chore_rejects_invalid_schedule_like_markdown(parse):
    with pytest.raises(ValueError):
        ChoreFile.parse("---\nschedule: not-a-rule\n---\nClean up.", name="cleanup")
    with pytest.raises(ToolangValidationError, match="schedule"):
        parse("chore cleanup:\n  schedule = not-a-rule\n  Clean up.\n")


def test_name_identity_shadowing_and_collision(tmp_path, monkeypatch):
    import pytest
    from dataclasses import replace
    from toolang.lang.ast import Program
    from toolang.work import state

    program = Program.from_source(
        "chore cleanup:\n  First.\nchore cleanup:\n  Second.\n"
    )
    first, second = state.inline_chores(program.chores, path=tmp_path / "agent.too")
    capitalized = replace(first, name="Cleanup")
    assert first.id == second.id != capitalized.id
    assert len(first.id) == 16
    assert state.merge_chores((first, second))[first.id] == second
    file = ChoreFile.parse("First.", name="cleanup", path=tmp_path / "cleanup.md")
    assert state.file_chore(file).body_hash == first.body_hash
    assert file.id == first.id
    monkeypatch.setattr(state, "chore_id", lambda _: "0" * 16)
    with pytest.raises(ValueError, match="collision"):
        state.merge_chores((first, capitalized))
