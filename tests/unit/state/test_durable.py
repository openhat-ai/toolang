from __future__ import annotations

from pathlib import Path
from typing import cast

from toolang.state.caps import durable_entries_snapshot
from toolang.state.durable import scan_durable_state


def test_program_load_uses_captured_snapshot_content(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    program_path = root / "agents" / "alice" / "agent.too"
    program_path.parent.mkdir(parents=True)
    program_path.write_text("agent alice\n\nagic:\n  First.\n", encoding="utf-8")
    snapshot = scan_durable_state(root, "alice")

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
    snapshot = scan_durable_state(root, "alice")

    prompt_path.write_text(
        "---\ndescription: Second style\n---\nUse the second style.\n",
        encoding="utf-8",
    )
    config_path.write_text(
        '[prompts]\nnew = { ref = "github://acme/agents/prompts/new.md@main" }\n',
        encoding="utf-8",
    )

    projected = durable_entries_snapshot(snapshot)
    shared = cast(list[dict[str, object]], projected["shared_entries"])
    private = cast(list[dict[str, object]], projected["private_entries"])
    assert shared[0]["meta"] == {"description": "First style"}
    assert private[0]["name"] == "old"
    assert private[0]["ref"] == "github://acme/agents/prompts/old.md@main"
