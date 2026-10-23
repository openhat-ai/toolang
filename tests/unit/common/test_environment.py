import pytest

from toolang.common.environment import (
    DEFAULT_KEEP,
    DEFAULT_PICK,
    EnvironmentPolicy,
    apply_environment_file,
    current_environment,
    environment_scope,
    resolve_agent_environment,
    resolve_root_environment,
)
from toolang.common.layout import AgentLayout


def test_root_without_file_retains_shell_and_agent_selects_one_provider(tmp_path):
    shell = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "first",
        "ANTHROPIC_API_KEY": "second",
        "UNRELATED": "private",
    }
    root = resolve_root_environment(tmp_path, shell)
    assert dict(root.values) == shell
    agent = resolve_agent_environment(root, AgentLayout.resident(tmp_path, "alice"))
    assert dict(agent.values) == {"PATH": "/bin", "OPENAI_API_KEY": "first"}
    assert agent.picked == "OPENAI_API_KEY"
    assert "first" not in repr(agent)


def test_root_file_restricts_inheritance_and_supplies_central_values(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=central\nPATH=/custom\nEMPTY=\n")
    shell = {"PATH": "/bin", "OPENAI_API_KEY": "excluded", "UNRELATED": "private"}
    root = resolve_root_environment(tmp_path, shell)
    assert dict(root.values) == {
        "PATH": "/custom",
        "ANTHROPIC_API_KEY": "central",
        "EMPTY": "",
    }
    (tmp_path / ".alice.env").write_text("OPENAI_API_KEY\nANTHROPIC_API_KEY\n")
    agent = resolve_agent_environment(root, AgentLayout.resident(tmp_path, "alice"))
    assert dict(agent.values) == {"PATH": "/custom", "ANTHROPIC_API_KEY": "central"}


def test_explicit_file_replaces_home_and_pick_but_preserves_keep(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.env.write_text("LOWER=excluded\nPATH=/lower\n")
    policy_file = tmp_path / ".alice.env"
    policy_file.write_text("HOME\nPATH=/selected\nOPTION=\nLITERAL='${HOME}'\n")
    root = resolve_root_environment(
        tmp_path, {"PATH": "/bin", "HOME": "/home", "OPENAI_API_KEY": "excluded"}
    )
    agent = resolve_agent_environment(root, layout)
    assert dict(agent.values) == {
        "PATH": "/selected",
        "HOME": "/home",
        "OPTION": "",
        "LITERAL": "${HOME}",
    }
    assert agent.source == policy_file
    assert agent.picked is None
    policy_file.write_text("")
    assert dict(resolve_agent_environment(root, layout).values) == {
        "PATH": "/bin",
        "HOME": "/home",
    }


def test_resident_policy_survives_home_recreation(tmp_path):
    (tmp_path / ".alice.env").write_text("SELECTED=fixed\n")
    root = resolve_root_environment(tmp_path, {})
    first = resolve_agent_environment(root, AgentLayout.resident(tmp_path, "alice"))
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    assert resolve_agent_environment(root, layout).values == first.values


def test_home_file_is_a_supported_resident_fallback(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.env.write_text("ANTHROPIC_API_KEY\n")
    root = resolve_root_environment(
        tmp_path, {"ANTHROPIC_API_KEY": "selected", "OPENAI_API_KEY": "not-picked"}
    )
    assert dict(resolve_agent_environment(root, layout).values) == {
        "ANTHROPIC_API_KEY": "selected"
    }


def test_roaming_uses_only_source_local_toolang_env(tmp_path):
    root_path = tmp_path / "root"
    root_path.mkdir()
    source = tmp_path / "source" / "aid.too"
    source.parent.mkdir()
    source.write_text("agic:\n  Hello.\n")
    (tmp_path / ".toolang.env").write_text("ANCESTOR=excluded\n")
    (source.parent / ".env").write_text("PROJECT=excluded\n")
    (source.parent / ".toolang.env").write_text("LOCAL=included\n")
    (root_path / ".aid.env").write_text("RESIDENT=excluded\n")
    root = resolve_root_environment(root_path, {})
    layout = AgentLayout.roaming(source, root=root_path)
    agent = resolve_agent_environment(root, layout)
    assert dict(agent.values) == {"LOCAL": "included"}
    assert agent.source == source.parent / ".toolang.env"


def test_visiting_uses_default_without_same_name_resident_grant(tmp_path):
    (tmp_path / ".alice.env").write_text("RESIDENT=excluded\n")
    root = resolve_root_environment(
        tmp_path, {"OPENAI_API_KEY": "selected", "UNRELATED": "excluded"}
    )
    layout = AgentLayout.visiting(
        "https://example.invalid/alice.too", "alice", root=tmp_path
    )
    agent = resolve_agent_environment(root, layout)
    assert dict(agent.values) == {"OPENAI_API_KEY": "selected"}


def test_configured_lists_replace_defaults_and_skip_blank_values(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[env]\nkeep=[]\npick=["EMPTY", "BLANK", "CUSTOM", "OTHER"]\n'
    )
    root = resolve_root_environment(
        tmp_path,
        {
            "PATH": "/bin",
            "EMPTY": "",
            "BLANK": "  ",
            "CUSTOM": " selected ",
            "OTHER": "excluded",
        },
    )
    assert dict(
        resolve_agent_environment(root, AgentLayout.resident(tmp_path, "alice")).values
    ) == {"CUSTOM": " selected "}
    assert EnvironmentPolicy.from_config({}, tmp_path).keep == DEFAULT_KEEP
    assert (
        EnvironmentPolicy.from_config({"env": {"keep": []}}, tmp_path).pick
        == DEFAULT_PICK
    )


@pytest.mark.parametrize(
    "config",
    [
        {"env": {"TOKEN": "do-not-print"}},
        {"env": {"keep": "do-not-print"}},
        {"env": {"pick": ["INVALID=do-not-print"]}},
        {"env": {"keep": ["DUP", "DUP"]}},
    ],
)
def test_invalid_configuration_never_echoes_values(tmp_path, config):
    with pytest.raises(ValueError) as error:
        EnvironmentPolicy.from_config(config, tmp_path / "config.toml")
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize(
    "content", ["=do-not-print\n", "A=do-not-print\nA\n", "A='do-not-print\n"]
)
def test_invalid_file_fails_with_location_without_values(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content)
    with pytest.raises(ValueError) as error:
        apply_environment_file(path, {})
    assert str(path) in str(error.value)
    assert "do-not-print" not in str(error.value)


def test_local_toml_cannot_define_environment_values(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.config.write_text('[env]\nTOKEN="do-not-print"\n')
    with pytest.raises(ValueError, match="only in the Toolang root") as error:
        from toolang.common.config_sources import config_sources

        config_sources(layout)
    assert "do-not-print" not in str(error.value)


def test_environment_scope_installs_snapshot_and_restores_on_failure(
    tmp_path, monkeypatch
):
    import os

    monkeypatch.setenv("PRIVATE_SENTINEL", "excluded")
    before = dict(os.environ)
    root = resolve_root_environment(tmp_path, {"PATH": "/bin"})
    with pytest.raises(RuntimeError), environment_scope(root):
        assert dict(os.environ) == {"PATH": "/bin"}
        assert current_environment() is root
        raise RuntimeError("failed command")
    assert dict(os.environ) == before
    assert current_environment() is None


def test_selection_provenance_does_not_log_values(tmp_path, caplog):
    import logging

    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "synthetic-secret"})
    agent = resolve_agent_environment(root, AgentLayout.resident(tmp_path, "alice"))
    with caplog.at_level(logging.INFO), environment_scope(agent):
        pass
    assert "OPENAI_API_KEY" in caplog.text
    assert "synthetic-secret" not in caplog.text


def test_unreadable_agent_file_does_not_fall_back(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.env.write_text("FALLBACK=excluded\n")
    (tmp_path / ".alice.env").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="could not read environment file"):
        resolve_agent_environment(resolve_root_environment(tmp_path, {}), layout)
