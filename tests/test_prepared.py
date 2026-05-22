from __future__ import annotations

import json
from pathlib import Path

from toolang.state.prepared import load_shared_lock


def test_load_shared_lock_reads_binding_source_entries(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    prepared_dir = toolang_root / ".caps"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    (prepared_dir / "lock.json").write_text(
        json.dumps(
            {
                "visibility": "shared",
                "updated_at": "2026-04-18T00:00:00Z",
                "fingerprint": "abc",
                "input_fingerprint": "input",
                "entries": [
                    {
                        "kind": "skill",
                        "name": "pdf-processing",
                        "shape": "dir",
                        "ref": "github://by3gus/agents/skills/pdf-processing@main",
                        "path": ".caps/wired/skills/pdf-processing/SKILL.md",
                        "source": {
                            "origin": "remote",
                            "binding": "wired",
                            "path": "config.toml",
                            "updated_at": "2026-04-18T00:00:00Z",
                            "fingerprint": "def",
                        },
                        "meta": {"remote": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    lock = load_shared_lock(toolang_root)

    assert len(lock.entries) == 1
    assert lock.entries[0].ref == "github://by3gus/agents/skills/pdf-processing@main"
    assert lock.entries[0].source.binding == "wired"
