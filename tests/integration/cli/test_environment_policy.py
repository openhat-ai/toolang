"""Environment policy at public CLI and user-code boundaries."""

import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from toolang.catalog.agent import LocalAgents
from toolang.cli.toolang.main import app
from toolang.cli.toolang.commands import script
from toolang.common.github import GitHubRef
from toolang.setup import tools as setup_tools
from toolang.up import source as sources

from tests.support.layout import root_file


@pytest.fixture
def shell_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    values = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": "synthetic-first",
        "ANTHROPIC_API_KEY": "synthetic-second",
        "PRIVATE_SENTINEL": "synthetic-private",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("root_file", [False, True])
def test_tool_factories_and_children_receive_the_selected_environment(
    tmp_path, monkeypatch, shell_environment, agent, root_file
):
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")
    if root_file:
        (root / ".env").write_text("ANTHROPIC_API_KEY=central\n")
    observed = []
    original = setup_tools.load_toolsets_with_sources

    def factory(**kwargs):
        # This represents third-party factory code, including its own subprocesses.
        observed.append(dict(os.environ))
        child = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import json,os; print(json.dumps(dict(os.environ)))",
            ],
            text=True,
        )
        observed.append(json.loads(child))
        return original(**kwargs)

    monkeypatch.setattr(setup_tools, "load_toolsets_with_sources", factory)
    shell_environment = dict(os.environ)
    args = ["--root", str(root), *(["alice"] if agent else []), "tools", "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.stderr
    kept = {name: shell_environment[name] for name in ("PATH", "HOME")}
    expected = (
        {**kept, "ANTHROPIC_API_KEY": "central"}
        if root_file
        else {**kept, "ANTHROPIC_API_KEY": "synthetic-second"}
        if agent
        else shell_environment
    )
    assert observed[0] == expected
    # Python may add LC_CTYPE when its supplied environment lacks a locale.
    assert {
        key: value
        for key, value in observed[1].items()
        if key not in {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    } == expected
    assert dict(os.environ) == shell_environment
    assert "synthetic-private" not in result.output


@pytest.mark.parametrize("selected_file", ["auto", "local", "root"])
def test_script_entry_uses_roaming_policy_before_execution(
    tmp_path, monkeypatch, shell_environment, selected_file
):
    root = tmp_path / "root"
    source = tmp_path / "main.too"
    source.write_text("flow main():\n  pass\n")
    from toolang.common.layout import AgentLayout

    layout = AgentLayout.roaming(source, root=root)
    layout.agent_home.mkdir(parents=True)
    if selected_file != "auto":
        layout.env.write_text("OPENAI_API_KEY\n")
    if selected_file == "root":
        (layout.agent_root / ".main.env").write_text("PRIVATE_SENTINEL\n")
    observed = []

    def execute(*_args, **_kwargs):
        observed.append(dict(os.environ))
        return 0

    monkeypatch.setattr(script, "_run", execute)
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    shell_environment = dict(os.environ)
    result = CliRunner().invoke(app, [str(source), "main"])
    assert result.exit_code == 0, result.stderr
    provider = {
        "auto": "ANTHROPIC_API_KEY",
        "local": "OPENAI_API_KEY",
        "root": "PRIVATE_SENTINEL",
    }[selected_file]
    assert observed == [
        {name: shell_environment[name] for name in ("PATH", "HOME", provider)}
    ]
    assert dict(os.environ) == shell_environment


@pytest.mark.parametrize(
    "selector, identity",
    [
        (
            "https://example.invalid/alice.too?version=1+2%2F3",
            "https://example.invalid/alice.too?version=1+2%2F3",
        ),
        (
            "https://github.com/owner/repo/blob/main/alice.too",
            "github://owner/repo/alice.too@main",
        ),
        ("github:owner/alice", "gh:owner/alice"),
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_visiting_factories_use_requested_identity_before_and_after_fetch(
    tmp_path, monkeypatch, shell_environment, selector, identity, explicit
):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".alice.env").write_text("RESIDENT=excluded\n")
    pinned_ref = GitHubRef("owner", "repo", "alice.too", "a" * 40)
    layout = sources.visiting_layout(selector, toolang_root=root)
    if explicit:
        from toolang.up.identity import visiting_home_guard

        with visiting_home_guard(layout):
            (layout.agent_root / ".alice.env").write_text("OPENAI_API_KEY\n")
    monkeypatch.setattr(
        sources, "_resolve_github_agent_shorthand", lambda *_a, **_kw: pinned_ref
    )
    monkeypatch.setattr(sources, "_resolve_github_commit", lambda _ref: pinned_ref.rev)
    fetched = []

    def fetch(ref, **_kwargs):
        fetched.append((ref.render(), dict(os.environ)))
        return "flow main():\n  pass\n"

    monkeypatch.setattr(sources, "fetch_agent_ref", fetch)
    observed = []
    original = setup_tools.load_toolsets_with_sources

    def factory(**kwargs):
        observed.append(dict(os.environ))
        return original(**kwargs)

    monkeypatch.setattr(setup_tools, "load_toolsets_with_sources", factory)
    caller = dict(os.environ)
    result = CliRunner().invoke(app, ["--root", str(root), selector, "tools", "--json"])
    assert result.exit_code == 0, result.stderr
    provider = "OPENAI_API_KEY" if explicit else "ANTHROPIC_API_KEY"
    expected = {name: caller[name] for name in ("PATH", "HOME", provider)}
    assert len(fetched) == 1
    assert fetched[0][0] == (
        identity
        if selector.startswith("https://example.invalid")
        else pinned_ref.render()
    )
    assert fetched[0][1] == expected
    assert observed == [expected]
    assert dict(os.environ) == caller


def test_invalid_agent_file_fails_without_fallback_or_secret_output(
    tmp_path, shell_environment
):
    root = tmp_path / "root"
    home = LocalAgents(root / "agents").create("alice", content="")
    (root / ".alice.env").write_text("TOKEN='synthetic-secret\n")
    (home / ".env").write_text("TOKEN=fallback\n")
    shell_environment = dict(os.environ)
    result = CliRunner().invoke(app, ["--root", str(root), "alice", "tools"])
    assert result.exit_code == 1
    assert ".alice.env:1" in "".join(result.stderr.replace("│", "").split())
    assert "synthetic-secret" not in result.output
    assert dict(os.environ) == shell_environment


def test_invalid_visiting_file_does_not_print_source_url_credentials(
    tmp_path, shell_environment
):
    root = tmp_path / "root"
    root.mkdir()
    ref = "https://user:private-userinfo@example.invalid/alice.too?token=private-query#private-fragment"
    layout = sources.visiting_layout(ref, toolang_root=root)
    from toolang.up.identity import visiting_home_guard

    with visiting_home_guard(layout):
        (layout.agent_root / ".alice.env").write_text("TOKEN='private-value\n")
    caller = dict(os.environ)
    result = CliRunner().invoke(app, ["--root", str(root), ref, "tools"])
    assert result.exit_code == 1
    output = "".join(result.output.replace("│", "").split())
    assert ".alice.env:1" in output
    for marker in (
        "private-userinfo",
        "private-query",
        "private-fragment",
        "private-value",
    ):
        assert marker not in output
    assert dict(os.environ) == caller


def test_shell_tool_child_receives_the_agent_snapshot(tmp_path):
    import asyncio
    from toolang.base.types.tool import ToolContext
    from toolang.common.environment import (
        resolve_root_environment,
        resolve_agent_environment,
        environment_scope,
    )
    from toolang.common.layout import AgentLayout
    from toolang.plugin.toolsets.shell import ShellToolset

    root = resolve_root_environment(
        tmp_path,
        {
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "OPENAI_API_KEY": "synthetic",
            "PRIVATE_SENTINEL": "excluded",
        },
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    context = ToolContext(
        home=tmp_path, room=tmp_path, workspaces={"repo": tmp_path}, cwd="repo://"
    )
    with environment_scope(resolve_agent_environment(root, layout)):
        tool = ShellToolset({}).tools()["execute"]
        result = asyncio.run(
            tool.invoke(
                {
                    "command": 'test "${PRIVATE_SENTINEL+set}" = "" && test "$OPENAI_API_KEY" = synthetic'
                },
                context,
            )
        )
    assert result.error is None
    assert result.output["exit_code"] == 0


@pytest.mark.parametrize("path", [None, "", "/authorized/bin"])
@pytest.mark.parametrize("placement", ["resident", "roaming", "visiting"])
def test_guest_launch_uses_common_dotenv_priority_and_explicit_keep(
    tmp_path, monkeypatch, shell_environment, path, placement
):
    from dataclasses import replace
    from toolang.common.environment import agent_environment_paths, current_environment
    from toolang.common.layout import AgentLayout
    from toolang.plugin.sandboxes.docker._guest import write_guest_env
    from toolang.setup.config import load_setup_envs
    from toolang.up import server

    root = tmp_path / "root"
    root.mkdir()
    if placement == "resident":
        layout = AgentLayout.resident(root, "alice")
    elif placement == "roaming":
        source = tmp_path / "alice.too"
        source.write_text("")
        layout = AgentLayout.roaming(source, root=root)
    else:
        layout = replace(
            AgentLayout.visiting(
                "https://example.invalid/alice.too", "alice", root=root
            ),
            place=tmp_path / "visit",
        )
    root = layout.agent_root
    root_file(root, "config.toml").write_text(
        '[env]\nkeep=["PATH", "CARGO_HOME", "RUSTUP_HOME", "UV_TOOL_DIR", '
        '"UV_NO_PROGRESS", "PYTHONPATH", "IMAGE_CUSTOM"]\npick=["OPENAI_API_KEY"]\n'
    )
    layout.agent_home.mkdir(parents=True, exist_ok=True)
    layout.env.write_text("LOWER_PRIORITY=must-not-load\nADMITTED=wrong\n")
    selected_file = agent_environment_paths(layout)[0]
    values = (
        {}
        if path is None
        else {
            "ADMITTED": '${HOME}/"literal"/\\tail\nnext',
            "PATH": path,
            "TOOLANG_ROOT": "/spoofed/root",
            "HOSTNAME": "spoofed",
            "TOOLANG_SANDBOX_INSTANCE": "spoofed",
        }
    )
    write_guest_env(selected_file, dotenv_envs=values, process_envs={})
    native = {
        "CARGO_HOME": "/usr/local/cargo",
        "RUSTUP_HOME": "/usr/local/rustup",
        "UV_TOOL_DIR": "/image/tools",
        "UV_NO_PROGRESS": "0",
        "PYTHONPATH": "/image/python",
        "IMAGE_CUSTOM": "preserved",
        "HOSTNAME": "0123456789ab",
        "TOOLANG_SANDBOX_INSTANCE": "0123456789ab",
    }
    for name, value in native.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["/runtime/bin/too"])
    observed = []

    def serve(spec, *, environ, **_kwargs):
        observed.append(dict(environ))
        assert dict(os.environ) == environ
        active = current_environment()
        assert active is not None
        assert active.source == selected_file
        assert active.picked is None
        assert spec.layout.placement == placement
        # Existing setup refreshes must keep the startup mapping.
        selected_file.write_text("ADMITTED=changed\nNEW_VALUE=must-not-load\n")
        assert load_setup_envs(spec.layout) == environ
        child = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import json,os;print(json.dumps(dict(os.environ)))",
                ],
                text=True,
            )
        )
        assert all(child[name] == value for name, value in environ.items())
        return 0

    monkeypatch.setattr(server, "serve", serve)
    args = [
        "--root",
        str(root),
        "_serve",
        "alice",
        "--place",
        str(root),
        "--location",
        "guest",
        "--sandbox",
        "docker:test",
        "--placement",
        placement,
        "--registry-directory",
        str(root / ".runtime/active"),
        "--registry-status",
        str(layout.runtime_status),
    ]
    if layout.source_path is not None:
        args += ["--source-path", str(layout.source_path)]
    if layout.source_ref is not None:
        args += ["--source-ref", layout.source_ref]
    caller = dict(os.environ)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert dict(os.environ) == caller
    assert len(observed) == 1
    env = observed[0]
    assert all(env[name] == value for name, value in native.items())
    assert "LOWER_PRIORITY" not in env
    assert "NEW_VALUE" not in env
    assert env.get("ADMITTED") == values.get("ADMITTED")
    assert env["TOOLANG_ROOT"] == str(root)
    assert env["TOOLANG_SANDBOX"] == "docker:test"
    selected_path = caller["PATH"] if path is None else path
    assert env["PATH"] == "/runtime/bin" + (
        ":" + selected_path if selected_path else ""
    )


@pytest.mark.parametrize("root_policy", [None, "ADMITTED=root-value\n", ""])
@pytest.mark.parametrize("internal", [False, True])
def test_host_serve_reduces_environment_with_an_authored_home_dotenv(
    tmp_path, monkeypatch, shell_environment, root_policy, internal
):
    from toolang.common.environment import current_environment
    from toolang.setup.config import load_setup_envs
    from toolang.up import sandbox, server

    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")
    (root_file(root, "config.toml")).write_text('[env]\nkeep=["PATH", "HOME"]\n')
    (root / "agents/alice/.env").write_text("ADMITTED=home-value\n")
    if root_policy is not None:
        (root / ".alice.env").write_text(root_policy)
    observed = []

    def captured(spec, *, environ, **_kwargs):
        assert dict(os.environ) == environ
        active = current_environment()
        assert active is not None
        assert load_setup_envs(spec.layout) == environ
        assert active.picked is None
        observed.append(dict(environ))
        child = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import json,os; print(json.dumps(dict(os.environ)))",
                ],
                text=True,
            )
        )
        assert "PRIVATE_SENTINEL" not in child
        assert "OPENAI_API_KEY" not in child
        assert "ANTHROPIC_API_KEY" not in child
        return 0

    monkeypatch.setattr(sandbox, "prepare_launch_state", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_serve_captured", captured)
    caller = dict(os.environ)
    operands = ["_serve", "alice"] if internal else ["alice", "serve"]
    result = CliRunner().invoke(app, ["--root", str(root), *operands])
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert dict(os.environ) == caller
    assert len(observed) == 1
    env = observed[0]
    assert env.get("ADMITTED") == (
        "home-value" if root_policy is None else "root-value" if root_policy else None
    )
    assert "PRIVATE_SENTINEL" not in env
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == caller["PATH"]


@pytest.mark.parametrize("path", [None, "", "/authorized/bin"])
@pytest.mark.parametrize("internal", [False, True])
def test_host_serve_preserves_an_explicit_or_absent_path(
    tmp_path, monkeypatch, shell_environment, path, internal
):
    from toolang.up import sandbox, server

    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")
    (root_file(root, "config.toml")).write_text('[env]\nkeep=["HOME"]\n')
    (root / ".alice.env").write_text("" if path is None else f"PATH={path}\n")
    observed = []

    def captured(_spec, *, environ, **_kwargs):
        observed.append(dict(environ))
        return 0

    monkeypatch.setattr(sandbox, "prepare_launch_state", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_serve_captured", captured)
    operands = ["_serve", "alice"] if internal else ["alice", "serve"]
    result = CliRunner().invoke(app, ["--root", str(root), *operands])
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert len(observed) == 1
    assert observed[0].get("PATH") == path


@pytest.mark.parametrize(
    "changed", ["ADMITTED=changed\nNEW_SECRET=excluded\n", "", "BROKEN='\n"]
)
def test_managed_host_exec_uses_the_controller_environment_snapshot(
    tmp_path, monkeypatch, shell_environment, changed
):
    from toolang.base.types.sandbox import SandboxRequest
    from toolang.common.environment import (
        resolve_agent_environment,
        resolve_root_environment,
    )
    from toolang.common.layout import AgentLayout
    from toolang.plugin.sandboxes.host import HostSandbox, HOST_LAUNCH_ENV
    from toolang.setup.config import load_setup_envs
    from toolang.up import server

    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")
    layout = AgentLayout.resident(root, "alice")
    policy = root / ".alice.env"
    policy.write_text("ADMITTED=selected\n")
    selected = resolve_agent_environment(
        resolve_root_environment(root, dict(os.environ)), layout
    )
    spec = server.resolve_serve(layout=layout, port=8123, launch_id="managed-launch")
    plan = HostSandbox({}).prepare(
        None,
        SandboxRequest(
            local_place=root,
            local_home=layout.home,
            hosted_place=root,
            hosted_home=layout.home,
            agent_name="alice",
            bind_host="127.0.0.1",
            endpoint_host="localhost",
            port=8123,
            endpoint="http://localhost:8123",
            command=("too", *server.build_serve_argv(spec)),
            working_directory=layout.home,
            output="inherit",
            log_path=None,
            envs={**selected.values, HOST_LAUNCH_ENV: "managed-launch"},
        ),
    )
    policy.write_text(changed)
    observed = []

    def captured(spec, *, environ, **_kwargs):
        observed.append(dict(environ))
        assert load_setup_envs(spec.layout) == environ
        return 0

    monkeypatch.setattr(server, "_serve_captured", captured)
    # Popen/exec replaces the inherited mapping, rather than overlaying it.
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in plan.envs.items():
        monkeypatch.setenv(name, value)
    result = CliRunner().invoke(app, list(plan.command[plan.command.index("--root") :]))
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert len(observed) == 1
    assert observed[0]["ADMITTED"] == "selected"
    assert observed[0]["PATH"] == selected.values["PATH"]
    assert "NEW_SECRET" not in observed[0]
    assert "PRIVATE_SENTINEL" not in observed[0]
    assert HOST_LAUNCH_ENV not in observed[0]


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("dotenv", [False, True])
def test_plain_config_patch_applies_to_ordinary_host_commands(
    tmp_path, monkeypatch, shell_environment, agent, dotenv
):
    root = tmp_path / "root"
    LocalAgents(root / "agents").create("alice", content="")
    raw = json.dumps({"plugin": {"toolset": {"shell": {"patch_probe": "configured"}}}})
    if dotenv:
        (root / ".env").write_text("TOOLANG_CONFIG_PATCH='" + raw + "'\n")
    else:
        monkeypatch.setenv("TOOLANG_CONFIG_PATCH", raw)
    observed = []
    original = setup_tools.load_toolsets_with_sources

    def factory(**kwargs):
        observed.append(kwargs["config"]["shell"]["patch_probe"])
        return original(**kwargs)

    monkeypatch.setattr(setup_tools, "load_toolsets_with_sources", factory)
    result = CliRunner().invoke(
        app, ["--root", str(root), *(["alice"] if agent else []), "tools", "--json"]
    )
    assert result.exit_code == 0, (result.stderr, result.exception)
    assert observed == ["configured"]
