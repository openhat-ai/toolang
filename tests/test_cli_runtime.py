from __future__ import annotations

from pathlib import Path

import toolang.cli.runtime as cli_runtime


def test_cli_runtime_delegates_to_agent_up(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_configure_logging(*, spec, environ) -> None:
        captured["log"] = spec
        captured["env"] = environ

    def fake_up(
        *,
        toolang_root: Path,
        agent_name: str,
        host: str,
        public_host: str | None,
        port: int | None,
        sandbox: str | None,
        models,
        dev: Path | None,
        sandbox_child: bool,
        loop_names,
        log_spec: str | None,
        environ,
    ) -> int:
        captured["toolang_root"] = toolang_root
        captured["agent_name"] = agent_name
        captured["host"] = host
        captured["public_host"] = public_host
        captured["port"] = port
        captured["sandbox"] = sandbox
        captured["models"] = tuple(models or ())
        captured["dev"] = dev
        captured["sandbox_child"] = sandbox_child
        captured["loop_names"] = tuple(loop_names or ())
        captured["log_spec"] = log_spec
        captured["up_environ"] = environ
        return 17

    monkeypatch.setattr(cli_runtime, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(cli_runtime.agent_up, "up", fake_up)

    result = cli_runtime.main(
        [
            "--root",
            "/tmp/toolang",
            "--agent",
            "alice",
            "--host",
            "0.0.0.0",
            "--public-host",
            "agent.example.com",
            "--port",
            "8765",
            "--sandbox",
            "none",
            "--model",
            "gpt-5",
            "--model",
            "o3",
            "--loop",
            "inspect",
            "--loop",
            "reload",
            "--dev",
            "/tmp/dist/toolang.whl",
            "--sandbox-child",
            "--log",
            "toolang.run=debug",
        ]
    )

    assert result == 17
    assert captured["log"] == "toolang.run=debug"
    assert captured["toolang_root"] == Path("/tmp/toolang")
    assert captured["agent_name"] == "alice"
    assert captured["host"] == "0.0.0.0"
    assert captured["public_host"] == "agent.example.com"
    assert captured["port"] == 8765
    assert captured["sandbox"] == "none"
    assert captured["models"] == ("gpt-5", "o3")
    assert captured["dev"] == Path("/tmp/dist/toolang.whl")
    assert captured["sandbox_child"] is True
    assert captured["loop_names"] == ("inspect", "reload")
    assert captured["log_spec"] == "toolang.run=debug"
