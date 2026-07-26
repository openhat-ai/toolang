from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.request import urlopen

from toolang.common.layout import AgentLayout
from toolang.up.hosting import HostingState


def test_none_hosting_start_health_and_stop(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agent alice\n", encoding="utf-8")
    port = _available_port()
    env = {**os.environ, "TOOLANG_ROOT": str(root)}
    base = (
        sys.executable,
        "-m",
        "toolang.cli.toolang",
        "--root",
        str(root),
    )

    try:
        started = subprocess.run(
            (*base, "start", "alice", "--sandbox", "none", "--port", str(port)),
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=40,
        )
        assert started.returncode == 0, started.stderr
        state = HostingState.load(layout.hosting_state)
        assert state is not None
        assert state.sandbox == "none"
        with urlopen(f"http://localhost:{port}/healthz", timeout=2) as response:
            assert response.status == 200

        stopped = subprocess.run(
            (*base, "stop", "alice"),
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert stopped.returncode == 0, stopped.stderr
        assert HostingState.load(layout.hosting_state) is None
    finally:
        state = HostingState.load(layout.hosting_state)
        if state is not None:
            subprocess.run(
                (*base, "stop", "alice", "--force"),
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])
