from __future__ import annotations

from pathlib import Path
from typing import cast

from toolang.state.state import authored_entries_snapshot
from toolang.state.source import read_authored_source


def test_program_load_uses_captured_snapshot_content(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    program_path = root / "agents" / "alice" / "agent.too"
    program_path.parent.mkdir(parents=True)
    program_path.write_text("agent alice\n\nagic:\n  First.\n", encoding="utf-8")
    snapshot = read_authored_source(root, "alice")

    program_path.write_text("agent alice\n\nagic:\n  Second.\n", encoding="utf-8")

    source = snapshot.load_program()
    assert source.source_text.endswith("agic:\n  First.\n")
    assert source.parse().agics[0].messages[0].content == "First."


def test_cap_projection_uses_captured_files(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    prompt_path = root / "prompts" / "style.md"
    config_path = root / "agents" / "alice" / "config.toml"
    prompt_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    prompt_path.write_text(
        "---\ndescription: First style\n---\nUse the first style.\n",
        encoding="utf-8",
    )
    config_path.write_text(
        '[prompts]\nold = { ref = "github://acme/agents/prompts/old.md@main" }\n',
        encoding="utf-8",
    )
    snapshot = read_authored_source(root, "alice")

    prompt_path.write_text(
        "---\ndescription: Second style\n---\nUse the second style.\n",
        encoding="utf-8",
    )
    config_path.write_text(
        '[prompts]\nnew = { ref = "github://acme/agents/prompts/new.md@main" }\n',
        encoding="utf-8",
    )

    projected = authored_entries_snapshot(snapshot)
    root_entries = cast(list[dict[str, object]], projected["root_entries"])
    home_entries = cast(list[dict[str, object]], projected["home_entries"])
    assert root_entries[0]["meta"] == {"description": "First style"}
    assert home_entries[0]["name"] == "old"
    assert home_entries[0]["ref"] == "github://acme/agents/prompts/old.md@main"
