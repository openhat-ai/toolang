from __future__ import annotations

from pathlib import Path

from toolang.agent_refs import resolve_agent_ref
from toolang.caps_view import load_prepared_caps
from toolang.layout import resolve_toolang_root
from toolang.prepared import prepare_agent

SOURCE_FIXTURE = Path(__file__).parent / "fixtures" / "source_only.too"


def test_load_prepared_caps_is_scoped_to_current_agent(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "team"
    home.mkdir(parents=True)
    (home / "alice.too").write_text(SOURCE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (home / "bob.too").write_text(
        """
service jira: ```md
Use this service for Jira.
```

prompt triage: ```md
Triage the issue.
```

thunk review:
    Review the issue.
""".strip(),
        encoding="utf-8",
    )

    prepared = prepare_agent(resolve_agent_ref("team/alice", cwd=tmp_path, toolang_root=root))
    caps = load_prepared_caps(prepared)

    assert [item.name for item in caps.services] == ["github"]
    assert [item.name for item in caps.prompts] == ["summarize"]
    assert [item.name for item in caps.psyches] == ["reviewer"]
