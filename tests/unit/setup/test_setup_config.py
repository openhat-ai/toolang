from __future__ import annotations

from pathlib import Path

import pytest

from toolang.base.types.run import RunLimits
from toolang.common.layout import AgentLayout
from toolang.setup.config import load_run_limits, load_setup_config, load_setup_envs


def test_setup_config_reads_only_the_toolang_root(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (root / "config.toml").write_text(
        '[models.providers.gateway]\nendpoint = "https://root.example/v1"\n',
        encoding="utf-8",
    )
    (home / "config.toml").write_text(
        '[models.providers.gateway]\nendpoint = "https://home.example/v1"\n',
        encoding="utf-8",
    )

    config = load_setup_config(AgentLayout.resident(root, "alice"))

    assert config == {
        "models": {"providers": {"gateway": {"endpoint": "https://root.example/v1"}}}
    }


def test_setup_envs_overlay_process_values_on_root_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "toolang"
    root.mkdir()
    (root / ".env").write_text(
        "ROOT_ONLY=from-root\nOVERRIDDEN=from-root\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OVERRIDDEN", "from-process")
    monkeypatch.setenv("PROCESS_ONLY", "from-process")

    envs = load_setup_envs(AgentLayout.resident(root, "alice"))

    assert envs["ROOT_ONLY"] == "from-root"
    assert envs["OVERRIDDEN"] == "from-process"
    assert envs["PROCESS_ONLY"] == "from-process"


def test_run_limits_overlay_agent_config_on_root_defaults(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    home.mkdir(parents=True)
    (root / "config.toml").write_text(
        """
[run.limits]
agic_model_calls = 50
tokens = 1000
cost = "2.5"
""",
        encoding="utf-8",
    )
    (home / "config.toml").write_text(
        """
[run.limits]
tokens = 2000
cost = "none"
time = 60
""",
        encoding="utf-8",
    )

    limits = load_run_limits(AgentLayout.resident(root, "alice"))

    assert limits == RunLimits(
        agic_model_calls=50,
        tokens=2000,
        cost=None,
        time=60,
    )


def test_run_limits_reject_unknown_and_negative_fields(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    root.mkdir()
    layout = AgentLayout.resident(root, "alice")
    (root / "config.toml").write_text(
        "[run.limits]\nturns = 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown run limit: turns"):
        load_run_limits(layout)

    (root / "config.toml").write_text(
        "[run.limits]\ncost = -1.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cost must be non-negative"):
        load_run_limits(layout)
