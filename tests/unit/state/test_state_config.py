from __future__ import annotations

from pathlib import Path

import pytest

from toolang.state.config import ConfiguredWorkspaces, canonical_state_config


def test_state_config_projection_excludes_setup_fields_and_is_canonical() -> None:
    first = canonical_state_config(
        b"""
[default]
model = "openai/one"

[allow]
models = ["openai/*"]
prompts = ["prompt/review"]

[prompts]
zeta = { ref = "acme/zeta" }
alpha = { ref = "acme/alpha" }
"""
    )
    reordered = canonical_state_config(
        b"""
[models.providers.openai]
adapter = "responses"

[prompts]
alpha = { ref = "acme/alpha" }
zeta = { ref = "acme/zeta" }

[allow]
prompts = ["prompt/review"]
tools = ["shell/*"]
"""
    )

    assert first == reordered
    text = first.decode()
    assert "default" not in text
    assert "models" not in text
    assert "tools" not in text
    assert text.index("alpha") < text.index("zeta")


def test_state_config_projection_includes_workspaces() -> None:
    assert canonical_state_config(
        b'[workspaces]\ntoolang = "/Users/alice/src/toolang"\n'
    ) == (
        b'__toolang_workspace_order__ = ["toolang"]\n\n'
        b'[workspaces]\ntoolang = "/Users/alice/src/toolang"\n'
    )


def test_canonical_workspace_config_retains_insertion_order_metadata() -> None:
    data = canonical_state_config(
        b'[workspaces]\nzeta = "/zeta"\nalpha = "/alpha"\n'
    ).decode()
    assert data.startswith(
        '__toolang_workspace_order__ = ["zeta", "alpha"]\n\n[workspaces]'
    )


def test_configured_workspaces_preserve_unrelated_toml_and_workspace_data(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    original = '# Keep this comment\n[default]\nmodel = "openai/gpt-5"\n'
    config.write_text(original, encoding="utf-8")
    workspace = tmp_path / "toolang"
    workspace.mkdir()
    configured = ConfiguredWorkspaces(config)

    assert configured.add(workspace) == ("toolang", str(workspace.resolve()))
    assert configured.list() == {"toolang": str(workspace.resolve())}
    assert config.read_text(encoding="utf-8").startswith(original)
    assert configured.remove("toolang") == str(workspace.resolve())

    assert workspace.is_dir()
    assert config.read_text(encoding="utf-8").startswith(original)


def test_configured_workspaces_preserve_unavailable_absolute_path_order() -> None:
    workspaces = ConfiguredWorkspaces.parse(
        '[workspaces]\nzeta = "/unavailable/zeta"\nalpha = "/unavailable/alpha"\n'
    )

    assert workspaces == {
        "zeta": "/unavailable/zeta",
        "alpha": "/unavailable/alpha",
    }
    assert tuple(workspaces) == ("zeta", "alpha")


def test_configured_workspaces_load_unresolvable_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "offline"

    def fail_resolve(_self: Path, *, strict: bool = False) -> Path:
        assert strict is False
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    assert ConfiguredWorkspaces.parse(f'[workspaces]\noffline = "{path}"\n') == {
        "offline": str(path)
    }


def test_configured_workspaces_reject_non_file_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.mkdir()

    with pytest.raises(ValueError, match="agent config must be a file"):
        ConfiguredWorkspaces(config).list()


def test_configured_workspaces_derive_name_from_resolved_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "toolang"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    assert ConfiguredWorkspaces(tmp_path / "config.toml").add(Path(".")) == (
        "toolang",
        str(workspace),
    )


def test_configured_workspaces_derive_name_from_parent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "toolang"
    child = workspace / "child"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)

    assert ConfiguredWorkspaces(tmp_path / "config.toml").add(Path("..")) == (
        "toolang",
        str(workspace),
    )


def test_configured_workspaces_normalize_derived_name(tmp_path: Path) -> None:
    workspace = tmp_path / "a_.b-c"
    workspace.mkdir()
    configured = ConfiguredWorkspaces(tmp_path / "config.toml")

    assert configured.add(workspace) == (
        "a-b-c",
        str(workspace),
    )
    assert configured.list() == {"a-b-c": str(workspace)}


def test_configured_workspaces_normalize_explicit_name(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    configured = ConfiguredWorkspaces(tmp_path / "config.toml")

    assert configured.add(workspace, name="  CaféSDK__Client  ") == (
        "cafe-sdk-client",
        str(workspace),
    )
    with pytest.raises(ValueError, match="workspace not found"):
        configured.remove("CAFÉ.SDK Client")
    assert configured.remove("cafe-sdk-client") == str(workspace)


@pytest.mark.parametrize("name", ("Straße", "项目 Repo"))
def test_configured_workspaces_reject_unsupported_name_characters(
    tmp_path: Path,
    name: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    with pytest.raises(ValueError, match="unsupported characters"):
        ConfiguredWorkspaces(tmp_path / "config.toml").add(workspace, name=name)


def test_configured_workspaces_reject_filesystem_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    alias = tmp_path / "alias"
    first.mkdir()
    alias.mkdir()
    configured = ConfiguredWorkspaces(tmp_path / "config.toml")
    configured.add(first, name="first")
    monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

    with pytest.raises(ValueError) as raised:
        configured.add(alias, name="alias")
    assert str(raised.value) == (
        f"workspace path already configured as first: {first.resolve()}"
    )


@pytest.mark.parametrize(
    ("content", "message"),
    (
        ('workspaces = "invalid"\n', "workspaces config must be a table"),
        ('[workspaces]\nBad_Name = "/tmp/repo"\n', "must use kebab case"),
        ('[workspaces]\nrepo = "relative/repo"\n', "workspace path must be absolute"),
    ),
)
def test_configured_workspaces_reject_invalid_config(
    content: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ConfiguredWorkspaces.parse(content)


def test_configured_workspaces_reject_duplicates_but_allow_nested_adds(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    root = tmp_path / "repo"
    nested = root / "nested"
    nested.mkdir(parents=True)
    configured = ConfiguredWorkspaces(config)
    configured.add(root, name="root")

    with pytest.raises(ValueError, match="workspace name already exists"):
        configured.add(tmp_path, name="ROOT")
    with pytest.raises(ValueError, match="workspace path already configured as root"):
        configured.add(root, name="duplicate")
    assert configured.add(nested, name="nested") == ("nested", str(nested.resolve()))
    assert configured.list() == {
        "nested": str(nested.resolve()),
        "root": str(root.resolve()),
    }


def test_workspace_order_survives_state_layer_roundtrip(tmp_path: Path) -> None:
    import tomlkit

    from toolang.common.layout import AgentLayout
    from toolang.state.prepare import prepare_agent_state
    from toolang.state.prepare import load_agent_state

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    first, last = tmp_path / "first", tmp_path / "last"
    first.mkdir()
    last.mkdir()
    layout.config.write_text(
        tomlkit.dumps({"workspaces": {"first": str(first), "last": str(last)}}),
        encoding="utf-8",
    )

    prepared = prepare_agent_state(layout)
    loaded = load_agent_state(layout, prepared.revision)

    assert tuple(prepared.workspaces) == ("first", "last")
    assert tuple(loaded.workspaces) == ("first", "last")
