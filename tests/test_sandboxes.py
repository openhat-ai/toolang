from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from toolang.base.protocols.sandbox import SandboxPlugin
from toolang.base.types.sandbox import SandboxSelector, SandboxStartRequest
from toolang.config.plugins import load_sandbox_binding
from toolang.up import create_sandbox_plugin


def test_create_none_sandbox_plugin_prepares_direct_plan(tmp_path: Path) -> None:
    plugin = create_sandbox_plugin("none", config={})

    assert isinstance(plugin, SandboxPlugin)
    plan = plugin.prepare(
        SandboxStartRequest(
            selector=SandboxSelector(driver="none"),
            local_root=tmp_path / "root",
            local_home=tmp_path / "root" / "agents" / "alice",
            sandbox_root=tmp_path / "root",
            sandbox_home=tmp_path / "root" / "agents" / "alice",
            agent_name="alice",
            bind_host="127.0.0.1",
            endpoint_host="127.0.0.1",
            port=8000,
            endpoint="http://127.0.0.1:8000",
            feature_names=("chat", "inspect"),
            run_command=("too", "run", "alice"),
            env_vars={"TOOLANG_ROOT": str(tmp_path / "root")},
        )
    )

    assert plan.start_mode == "direct"
    assert plan.selector.driver == "none"
    assert plan.run_command == ("too", "run", "alice")
    start = plugin.start(plan)
    assert start.state.selector.driver == "none"
    assert plugin.alive(start.state) is True


def test_create_docker_sandbox_plugin_prepares_and_starts(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def fake_remove(container_name: str) -> None:
        calls["removed"] = container_name

    def fake_run_detached(**kwargs) -> str:
        calls["run"] = kwargs
        return "container-123"

    monkeypatch.setattr("toolang.sandboxes.docker.docker_remove_container", fake_remove)
    monkeypatch.setattr("toolang.sandboxes.docker.docker_run_detached", fake_run_detached)
    monkeypatch.setattr("toolang.sandboxes.docker.docker_container_running", lambda name: name == "toolang-alice")

    root = tmp_path / "root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True, exist_ok=True)
    dev_artifact = root / "dist" / "toolang-0.1.0-py3-none-any.whl"
    dev_artifact.parent.mkdir(parents=True, exist_ok=True)
    dev_artifact.write_bytes(b"wheel")
    plugin = create_sandbox_plugin("docker", config={})

    plan = plugin.prepare(
        SandboxStartRequest(
            selector=SandboxSelector(driver="docker", target="python:3.13-slim"),
            local_root=root,
            local_home=home,
            sandbox_root=Path("/root/.toolang"),
            sandbox_home=Path("/root/.toolang/agents/alice"),
            agent_name="alice",
            bind_host="127.0.0.1",
            endpoint_host="127.0.0.1",
            port=8123,
            endpoint="http://127.0.0.1:8123",
            feature_names=("chat", "inspect"),
            run_command=("too", "run", "alice", "--port", "8123"),
            env_vars={"TOOLANG_ROOT": str(root)},
            local_dev_artifact=dev_artifact,
        )
    )

    assert plan.start_mode == "managed"
    assert plan.selector.driver == "docker"
    assert plan.selector.target == "python:3.13-slim"
    assert plan.state is not None
    assert plan.state.runtime_id == "toolang-alice"
    assert plan.sandbox_root == Path("/root/.toolang")
    assert plan.sandbox_home == Path("/root/.toolang/agents/alice")
    mounted_pairs = {(item.local_path, item.sandbox_path) for item in plan.mounts}
    assert (root, Path("/root/.toolang")) not in mounted_pairs
    assert (root / "config.toml", Path("/root/.toolang/config.toml")) in mounted_pairs
    assert (root / ".caps", Path("/root/.toolang/.caps")) in mounted_pairs
    assert (root / "psyches", Path("/root/.toolang/psyches")) in mounted_pairs
    assert (root / "skills", Path("/root/.toolang/skills")) in mounted_pairs
    assert (root / "services", Path("/root/.toolang/services")) in mounted_pairs
    assert (root / "prompts", Path("/root/.toolang/prompts")) in mounted_pairs
    assert (home, Path("/root/.toolang/agents/alice")) in mounted_pairs
    assert any(
        item.sandbox_path == Path("/root/.toolang/agents/alice/.state/sandbox")
        for item in plan.mounts
    )
    assert (root / "config.toml").is_file()
    assert (root / ".caps").is_dir()
    assert (root / ".sandbox" / "alice" / "start.json").is_file()
    assert (root / ".sandbox" / "alice" / "start.sh").is_file()
    script_text = (root / ".sandbox" / "alice" / "start.sh").read_text(encoding="utf-8")
    assert 'PYTHON_BIN=""' in script_text
    assert "ensure_uv" in script_text
    assert "uv tool run --from" in script_text
    assert " too run alice --port 8123" in script_text
    assert plan.sandbox_dev_artifact == Path("/root/.toolang/agents/alice/.state/sandbox") / dev_artifact.name

    start = plugin.start(plan)

    assert start.state.runtime_id == "toolang-alice"
    assert start.endpoint == "http://127.0.0.1:8123"
    assert calls["removed"] == "toolang-alice"
    run_call = cast(dict[str, Any], calls["run"])
    assert run_call["image"] == "python:3.13-slim"
    assert run_call["bind_host"] == "127.0.0.1"
    assert run_call["published_port"] == 8123
    assert run_call["env_values"]["TOOLANG_ROOT"] == "/root/.toolang"
    assert plugin.alive(start.state) is True


def test_create_docker_sandbox_plugin_prefixes_too_for_uv_tool_run(tmp_path: Path) -> None:
    root = tmp_path / "root"
    home = root / "agents" / "alice"
    home.mkdir(parents=True, exist_ok=True)
    plugin = create_sandbox_plugin("docker", config={})

    plugin.prepare(
        SandboxStartRequest(
            selector=SandboxSelector(driver="docker", target="python:3.13-slim"),
            local_root=root,
            local_home=home,
            sandbox_root=Path("/root/.toolang"),
            sandbox_home=Path("/root/.toolang/agents/alice"),
            agent_name="alice",
            bind_host="127.0.0.1",
            endpoint_host="127.0.0.1",
            port=8123,
            endpoint="http://127.0.0.1:8123",
            feature_names=("chat", "inspect"),
            run_command=("--root", "/root/.toolang", "run", "alice", "--port", "8123"),
            env_vars={"TOOLANG_ROOT": str(root)},
        )
    )

    script_text = (root / ".sandbox" / "alice" / "start.sh").read_text(encoding="utf-8")
    assert "uv tool run --from toolang too --root /root/.toolang run alice --port 8123" in script_text


def test_load_sandbox_binding_merges_root_and_agent_config(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    root.mkdir(parents=True, exist_ok=True)
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
    (root / "agents" / "alice").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "alice" / "config.toml").write_text(
        """
[sandbox.config]
image = "python:3.13-slim"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    binding = load_sandbox_binding(
        root,
        "alice",
        environ={"SANDBOX_TOKEN": "secret"},
    )

    assert binding is not None
    assert binding.selector.driver == "docker"
    assert binding.selector.target == "python:3.13"
    assert binding.config == {
        "image": "python:3.13-slim",
        "token": "secret",
    }
