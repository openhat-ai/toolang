from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from toolang.config.files import load_named_config, load_sandbox_config
from toolang.config.toml import load_optional_toml


def test_load_optional_toml_returns_empty_mapping_for_missing_file(
    tmp_path: Path,
) -> None:
    assert load_optional_toml(tmp_path / "missing.toml") == {}


def test_load_optional_toml_loads_nested_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[web]\nui_base_url = "https://example.com"\n', encoding="utf-8")

    assert load_optional_toml(path) == {
        "web": {"ui_base_url": "https://example.com"}
    }


def test_load_optional_toml_propagates_decode_errors(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[invalid", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        load_optional_toml(path)


def test_load_named_config_merges_root_and_agent_sections(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    root.mkdir(parents=True)
    (root / "config.toml").write_text(
        """
[tools.working_tree]
root = "/global"

[channels.telegram]
plugin = "telegram"
token_env = "TELEGRAM_BOT_TOKEN"
owner_chat_id = "100"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    agent_config = root / "agents" / "alice" / "config.toml"
    agent_config.parent.mkdir(parents=True)
    agent_config.write_text(
        """
[tools.working_tree]
root = "/agent"

[channels.telegram]
owner_chat_id = "123"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    tools = load_named_config(root, "alice", section="tools", environ={})
    channels = load_named_config(
        root,
        "alice",
        section="channels",
        environ={"TELEGRAM_BOT_TOKEN": "secret"},
    )

    assert tools == {"working_tree": {"root": "/agent"}}
    assert channels == {
        "telegram": {
            "plugin": "telegram",
            "token": "secret",
            "owner_chat_id": "123",
        }
    }


def test_load_named_config_reports_missing_environment_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    root.mkdir(parents=True)
    (root / "config.toml").write_text(
        """
[channels.telegram]
plugin = "telegram"
token_env = "TELEGRAM_BOT_TOKEN"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="channels.telegram.token_env.*TELEGRAM_BOT_TOKEN",
    ):
        load_named_config(root, "alice", section="channels", environ={})


def test_load_sandbox_config_merges_root_and_agent_sections(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    root.mkdir(parents=True)
    (root / "config.toml").write_text(
        """
[sandbox]
driver = "docker"
target = "python:3.13"

[sandbox.config]
token_env = "SANDBOX_TOKEN"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    agent_config = root / "agents" / "alice" / "config.toml"
    agent_config.parent.mkdir(parents=True)
    agent_config.write_text(
        """
[sandbox.config]
image = "python:3.13-slim"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_sandbox_config(
        root,
        "alice",
        environ={"SANDBOX_TOKEN": "secret"},
    )

    assert config == {
        "driver": "docker",
        "target": "python:3.13",
        "config": {
            "image": "python:3.13-slim",
            "token": "secret",
        },
    }


def test_load_sandbox_config_reports_missing_environment_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    root.mkdir(parents=True)
    (root / "config.toml").write_text(
        """
[sandbox]
driver = "docker"

[sandbox.config]
token_env = "SANDBOX_TOKEN"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="sandbox.config.token_env.*SANDBOX_TOKEN",
    ):
        load_sandbox_config(root, "alice", environ={})
