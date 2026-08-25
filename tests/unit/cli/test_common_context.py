from __future__ import annotations

from pathlib import Path

from toolang.cli.common.context import load_runtime_environ
from toolang.common.layout import AgentLayout


def test_runtime_environ_treats_dotenv_values_as_literals(
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.env.write_text("LITERAL='${HOME}/agent'\n", encoding="utf-8")

    environ = load_runtime_environ(
        layout,
        base_environ={
            "HOME": "/host/home",
        },
    )

    assert environ["LITERAL"] == "${HOME}/agent"
