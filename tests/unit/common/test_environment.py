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
from toolang.common.flatname import encode_flatname
from toolang.common.layout import AgentLayout


def test_default_environment_names_are_alphabetical():
    assert DEFAULT_KEEP == tuple(sorted(DEFAULT_KEEP))
    assert DEFAULT_PICK == tuple(sorted(DEFAULT_PICK))


@pytest.mark.parametrize(
    "name",
    [
        "ALIBABA_CODING_PLAN_API_KEY",
        "DASHSCOPE_API_KEY",
        "KIMI_API_KEY",
        "LLAMA_API_KEY",
        "META_MODEL_API_KEY",
        "MINIMAX_API_KEY",
        "MOONSHOT_API_KEY",
        "ZHIPU_API_KEY",
    ],
)
def test_default_pick_supports_additional_provider_credentials(tmp_path, name):
    agent = resolve_agent_environment(
        resolve_root_environment(tmp_path, {name: "synthetic-key"}),
        AgentLayout.resident(tmp_path, "alice"),
    )
    assert dict(agent.values) == {name: "synthetic-key"}
    assert dict(agent.guest_values) == {name: "synthetic-key"}
    assert agent.picked == name


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
    assert dict(agent.values) == {"PATH": "/bin", "ANTHROPIC_API_KEY": "second"}
    assert agent.picked == "ANTHROPIC_API_KEY"
    assert "second" not in repr(agent)


@pytest.mark.parametrize(
    ("content", "pick", "expected"),
    [
        (
            None,
            '"ANTHROPIC_API_KEY", "OPENAI_API_KEY"',
            {"ANTHROPIC_API_KEY": "second"},
        ),
        (None, '"OPENAI_API_KEY", "ANTHROPIC_API_KEY"', {"OPENAI_API_KEY": "first"}),
        ("", '"OPENAI_API_KEY"', {}),
        (
            "HOME=/guest-home\nCUSTOM\nOPENAI_API_KEY\n",
            '"ANTHROPIC_API_KEY"',
            {"HOME": "/guest-home", "CUSTOM": "host-custom", "OPENAI_API_KEY": "first"},
        ),
    ],
)
def test_guest_receives_only_pick_or_explicit_bindings(
    tmp_path, content, pick, expected
):
    (tmp_path / "config.toml").write_text(
        f'[env]\nkeep = ["HOME", "CUSTOM", "OPENAI_API_KEY"]\npick = [{pick}]\n'
    )
    if content is not None:
        (tmp_path / ".alice.env").write_text(content)
    agent = resolve_agent_environment(
        resolve_root_environment(
            tmp_path,
            {
                "HOME": "/host-home",
                "CUSTOM": "host-custom",
                "OPENAI_API_KEY": "first",
                "ANTHROPIC_API_KEY": "second",
            },
        ),
        AgentLayout.resident(tmp_path, "alice"),
    )
    assert agent.guest_values == expected
    assert agent.values["CUSTOM"] == "host-custom"
    assert agent.values["HOME"] == ("/guest-home" if content else "/host-home")
    assert "host-custom" not in repr(agent)


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


def test_roaming_falls_back_to_source_local_toolang_env(tmp_path):
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


@pytest.mark.parametrize("content", ["ANTHROPIC_API_KEY\nOPENAI_API_KEY\n", ""])
def test_roaming_root_file_wins_and_uses_only_root_values(tmp_path, content):
    root_path = tmp_path / "root"
    root_path.mkdir()
    (root_path / ".env").write_text("ANTHROPIC_API_KEY=central\n")
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    (source.parent / ".toolang.env").write_text("LOCAL=excluded\n")
    policy_file = root_path / f".{encode_flatname(str(source.resolve()))}.env"
    policy_file.write_text(content)
    root = resolve_root_environment(
        root_path, {"PATH": "/bin", "OPENAI_API_KEY": "excluded"}
    )
    agent = resolve_agent_environment(root, AgentLayout.roaming(source, root=root_path))
    assert dict(agent.values) == (
        {"PATH": "/bin", "ANTHROPIC_API_KEY": "central"}
        if content
        else {"PATH": "/bin"}
    )
    assert agent.source == policy_file
    assert agent.picked is None


def test_roaming_root_file_is_bound_to_canonical_source_not_name_or_home(
    tmp_path, monkeypatch
):
    source = tmp_path / "source" / "aid.too"
    source.parent.mkdir()
    source.write_text("flow main():\n  pass\n")
    alias = tmp_path / "alias.too"
    alias.symlink_to(source)
    other = tmp_path / "other" / source.name
    other.parent.mkdir()
    other.write_text(source.read_text())
    (tmp_path / ".aid.env").write_text("RESIDENT=excluded\n")
    policy_file = tmp_path / f".{encode_flatname(str(source.resolve()))}.env"
    policy_file.write_text("SELECTED=fixed\n")
    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "automatic"})
    monkeypatch.chdir(tmp_path)
    first = AgentLayout.roaming(alias.relative_to(tmp_path), root=tmp_path)
    second = AgentLayout.roaming(source, root=tmp_path)
    for layout in (first, second):
        assert not layout.home.exists()
        selected = resolve_agent_environment(root, layout)
        assert dict(selected.values) == {"SELECTED": "fixed"}
        assert selected.source == policy_file
    assert dict(
        resolve_agent_environment(
            root, AgentLayout.roaming(other, root=tmp_path)
        ).values
    ) == {"OPENAI_API_KEY": "automatic"}


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


@pytest.mark.parametrize(
    "selector, identity",
    [
        ("github:owner/alice", "gh:owner/alice"),
        ("gh:owner/alice", "gh:owner/alice"),
        (
            "gh:owner/repo/alice.too@main",
            "github://owner/repo/alice.too@main",
        ),
        (
            "https://github.com/owner/repo/blob/main/alice.too",
            "github://owner/repo/alice.too@main",
        ),
        (
            "https://raw.githubusercontent.com/owner/repo/main/alice.too",
            "github://owner/repo/alice.too@main",
        ),
        (
            " https://example.invalid/alice.too?version=1+2%2F3#top ",
            "https://example.invalid/alice.too?version=1+2%2F3#top",
        ),
    ],
)
def test_visiting_root_file_uses_canonical_requested_identity(
    tmp_path, selector, identity
):
    from toolang.up.source import visiting_layout

    policy_file = tmp_path / f".{encode_flatname(identity)}.env"
    policy_file.write_text("ANTHROPIC_API_KEY\n")
    (tmp_path / ".alice.env").write_text("RESIDENT=excluded\n")
    root = resolve_root_environment(
        tmp_path, {"OPENAI_API_KEY": "first", "ANTHROPIC_API_KEY": "selected"}
    )
    layout = visiting_layout(selector, toolang_root=tmp_path)
    agent = resolve_agent_environment(root, layout)
    assert dict(agent.values) == {"ANTHROPIC_API_KEY": "selected"}
    assert agent.source == policy_file
    assert agent.picked is None


def test_visiting_root_file_distinguishes_requested_urls(tmp_path):
    granted = "https://example.invalid/alice.too?version=1+2%2F3"
    (tmp_path / f".{encode_flatname(granted)}.env").write_text("GRANTED=selected\n")
    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "automatic"})
    for ref in (
        "https://other.invalid/alice.too?version=1+2%2F3",
        "https://example.invalid/alice.too?version=1+2/3",
        "https://example.invalid/alice.too?version=1%202%2F3",
    ):
        agent = resolve_agent_environment(
            root, AgentLayout.visiting(ref, "alice", root=tmp_path)
        )
        assert dict(agent.values) == {"OPENAI_API_KEY": "automatic"}


@pytest.mark.parametrize("placement", ["roaming", "visiting"])
def test_nonresident_without_source_does_not_borrow_resident_policy(
    tmp_path, placement
):
    (tmp_path / ".alice.env").write_text("RESIDENT=excluded\n")
    layout = AgentLayout(place=tmp_path, name="alice", placement=placement)
    layout.home.mkdir(parents=True)
    layout.env.write_text("HOME_VALUE=excluded\n")
    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "automatic"})
    assert dict(resolve_agent_environment(root, layout).values) == {
        "OPENAI_API_KEY": "automatic"
    }


@pytest.mark.parametrize("placement", ["roaming", "visiting"])
@pytest.mark.parametrize("invalid", [False, True])
def test_nonresident_root_file_errors_do_not_fall_back(tmp_path, placement, invalid):
    source = tmp_path / "alice.too"
    source.write_text("flow main():\n  pass\n")
    (tmp_path / ".toolang.env").write_text("LOCAL=excluded\n")
    ref = "gh:owner/alice"
    layout = (
        AgentLayout.roaming(source, root=tmp_path)
        if placement == "roaming"
        else AgentLayout.visiting(ref, "alice", root=tmp_path)
    )
    identity = str(source.resolve()) if placement == "roaming" else ref
    policy_file = tmp_path / f".{encode_flatname(identity)}.env"
    if invalid:
        policy_file.write_text("TOKEN='do-not-print\n")
    else:
        policy_file.symlink_to(tmp_path / "missing")
    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "excluded"})
    with pytest.raises(ValueError) as error:
        resolve_agent_environment(root, layout)
    assert str(policy_file) in str(error.value)
    assert "do-not-print" not in str(error.value)


@pytest.mark.parametrize("error_name", ["EACCES", "ENAMETOOLONG"])
def test_uninspectable_environment_file_fails_instead_of_using_automatic_policy(
    tmp_path, monkeypatch, error_name
):
    import errno
    from pathlib import Path
    import traceback

    ref = "https://user:private-userinfo@example.invalid/alice.too?token=private-query"
    layout = AgentLayout.visiting(ref, "alice", root=tmp_path)
    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "excluded"})
    policy_file = tmp_path / f".{encode_flatname(ref)}.env"
    original = Path.lstat

    def inspect(path):
        if path == policy_file:
            raise OSError(getattr(errno, error_name), "Cannot inspect file", str(path))
        return original(path)

    monkeypatch.setattr(Path, "lstat", inspect)
    with pytest.raises(ValueError, match="could not inspect environment file") as error:
        resolve_agent_environment(root, layout)
    output = "".join(traceback.format_exception(error.value, limit=0))
    assert "private-userinfo" not in output
    assert "private-query" not in output


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


def test_logging_handlers_receive_the_selected_environment(tmp_path, monkeypatch):
    import os
    from toolang.common import environment

    monkeypatch.setenv("PRIVATE_SENTINEL", "caller-private")
    monkeypatch.setenv("ADMITTED", "caller-value")
    selected = resolve_root_environment(tmp_path, {"ADMITTED": "startup-value"})
    observed = []

    def report(*_args):
        observed.append(
            (os.environ.get("PRIVATE_SENTINEL"), os.environ.get("ADMITTED"))
        )

    monkeypatch.setattr(environment.logger, "info", report)
    with environment_scope(selected):
        pass
    assert observed == [(None, "startup-value")]
    assert os.environ["PRIVATE_SENTINEL"] == "caller-private"


def test_logging_failure_restores_environment_context(tmp_path, monkeypatch):
    import os
    from contextvars import ContextVar
    from toolang.common import environment

    # Keep a cleanup failure from affecting other tests.
    monkeypatch.setattr(
        environment, "_CURRENT", ContextVar("test_environment", default=None)
    )
    monkeypatch.setenv("PRIVATE_SENTINEL", "caller-private")
    monkeypatch.setenv("ADMITTED", "caller-value")
    selected = resolve_root_environment(tmp_path, {"ADMITTED": "startup-value"})

    def fail(*_args):
        raise RuntimeError("logging failed")

    monkeypatch.setattr(environment.logger, "info", fail)
    with (
        pytest.raises(RuntimeError, match="logging failed"),
        environment_scope(selected),
    ):
        pytest.fail("logging failure must abort entry")
    assert current_environment() is None
    assert os.environ["PRIVATE_SENTINEL"] == "caller-private"
    assert os.environ["ADMITTED"] == "caller-value"


def test_unreadable_agent_file_does_not_fall_back(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.env.write_text("FALLBACK=excluded\n")
    (tmp_path / ".alice.env").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="could not read environment file"):
        resolve_agent_environment(resolve_root_environment(tmp_path, {}), layout)


def test_environment_file_rejects_fifo_before_opening_it(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    path = tmp_path / ".env"
    os.mkfifo(path)
    original = Path.open

    def open_file(candidate, *args, **kwargs):
        if candidate == path:
            pytest.fail("opening a FIFO could block environment resolution")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ValueError, match="could not read environment file"):
        resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "excluded"})


@pytest.mark.parametrize("local_file", [False, True])
def test_long_roaming_source_uses_local_or_automatic_policy(tmp_path, local_file):
    source = tmp_path / ("a" * 120) / ("b" * 120) / "alice.too"
    source.parent.mkdir(parents=True)
    source.write_text("flow main():\n  pass\n")
    if local_file:
        (source.parent / ".toolang.env").write_text("LOCAL=selected\n")
    root = resolve_root_environment(tmp_path, {"OPENAI_API_KEY": "automatic"})
    agent = resolve_agent_environment(root, AgentLayout.roaming(source, root=tmp_path))
    assert dict(agent.values) == (
        {"LOCAL": "selected"} if local_file else {"OPENAI_API_KEY": "automatic"}
    )


@pytest.mark.parametrize("root_exists", [False, True])
def test_long_visiting_url_uses_automatic_policy(tmp_path, root_exists):
    ref = "https://example.invalid/alice.too?version=" + "a" * 300
    root_path = tmp_path / "root"
    if root_exists:
        root_path.mkdir()
    root = resolve_root_environment(root_path, {"OPENAI_API_KEY": "automatic"})
    agent = resolve_agent_environment(
        root, AgentLayout.visiting(ref, "alice", root=root_path)
    )
    assert dict(agent.values) == {"OPENAI_API_KEY": "automatic"}


def test_source_url_credentials_are_private_in_environment_provenance(tmp_path, caplog):
    import logging

    ref = "https://user:private-userinfo@example.invalid/alice.too?token=private-query#private-fragment"
    policy_file = tmp_path / f".{encode_flatname(ref)}.env"
    policy_file.write_text("TOKEN=private-value\n")
    root = resolve_root_environment(tmp_path, {})
    agent = resolve_agent_environment(
        root, AgentLayout.visiting(ref, "alice", root=tmp_path)
    )
    with caplog.at_level(logging.INFO), environment_scope(agent):
        pass
    for marker in (
        "private-userinfo",
        "private-query",
        "private-fragment",
        "private-value",
    ):
        assert marker not in caplog.text
        assert marker not in repr(agent)
    assert agent.identity == f"visiting:{ref}"
    assert agent.source == policy_file


@pytest.mark.parametrize("invalid", [False, True])
def test_source_url_credentials_are_private_in_environment_file_errors(
    tmp_path, invalid
):
    import traceback

    ref = "https://user:private-userinfo@example.invalid/alice.too?token=private-query#private-fragment"
    policy_file = tmp_path / f".{encode_flatname(ref)}.env"
    if invalid:
        policy_file.write_text("TOKEN='private-value\n")
    else:
        policy_file.symlink_to(tmp_path / "missing")
    root = resolve_root_environment(tmp_path, {})
    with pytest.raises(ValueError) as error:
        resolve_agent_environment(
            root, AgentLayout.visiting(ref, "alice", root=tmp_path)
        )
    output = "".join(traceback.format_exception(error.value, limit=0))
    for marker in (
        "private-userinfo",
        "private-query",
        "private-fragment",
        "private-value",
    ):
        assert marker not in output
