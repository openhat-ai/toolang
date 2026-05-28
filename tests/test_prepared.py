from __future__ import annotations

import json
from pathlib import Path

from toolang.state.prepared import load_shared_lock


def test_load_shared_lock_reads_form_source_entries(tmp_path: Path) -> None:
    toolang_root = tmp_path / "toolang"
    prepared_dir = toolang_root / ".caps"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    (prepared_dir / "lock.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "built_at": "2026-04-18T00:00:00Z",
                "sources": {
                    "config": {
                        "path": "config.toml",
                        "shape": "file",
                        "mtime": 0,
                        "size": 12,
                        "fingerprint": "def",
                    }
                },
                "artifacts": {
                    "inline": {"path": ".caps/inline", "mtime": 0, "items": []},
                    "ref": {"path": ".caps/ref", "mtime": 0, "items": []},
                    "wired": {
                        "path": ".caps/wired",
                        "mtime": 0,
                        "items": [
                            {
                                "path": ".caps/wired/skills/pdf-processing",
                                "shape": "dir",
                                "mtime": 0,
                                "items": [
                                    {
                                        "path": ".caps/wired/skills/pdf-processing/SKILL.md",
                                        "shape": "file",
                                        "mtime": 0,
                                        "size": 4,
                                        "fingerprint": "abc",
                                    }
                                ],
                            }
                        ],
                    },
                },
                "prepared": {
                    "caps": [
                        {
                            "kind": "skill",
                            "name": "pdf-processing",
                            "form": "wired",
                            "source": "config",
                            "origin": {
                                "ref": "github://by3gus/agents/skills/pdf-processing@main"
                            },
                            "artifact": 0,
                            "object": {"meta": {"remote": True}, "content": ""},
                        }
                    ],
                    "tasks": [],
                    "chores": [],
                },
            }
        ),
        encoding="utf-8",
    )

    lock = load_shared_lock(toolang_root)

    assert len(lock.entries) == 1
    assert lock.entries[0].ref == "github://by3gus/agents/skills/pdf-processing@main"
    assert lock.entries[0].source.form == "wired"
    assert lock.entries[0].content == ""
