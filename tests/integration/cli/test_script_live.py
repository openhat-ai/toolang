"""Opt-in script CLI smoke tests using an explicitly selected real provider."""

from pathlib import Path
import subprocess
import sys

import pytest

from tests import PROJECT_ROOT
from tests.support.live_provider import LIVE_PROVIDER_SOURCE, LIVE_RESPONSE_PREFIX

pytestmark = pytest.mark.live_provider


@pytest.mark.parametrize("runnable", ["smoke", "relay"])
def test_script_cli_calls_real_provider(tmp_path: Path, request, runnable: str):
    model = request.config.getoption("--live-model")
    if not model:
        pytest.skip("pass --live-model to run real-provider script tests")
    source = tmp_path / "smoke.too"
    source.write_text(LIVE_PROVIDER_SOURCE, encoding="utf-8")
    marker = f"TOOLANG_SCRIPT_{runnable.upper()}"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "toolang.cli.toolang",
            str(source),
            runnable,
            "--model",
            model,
            "--quiet",
            "--out",
            "-",
            "--",
            marker,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{LIVE_RESPONSE_PREFIX} {marker}"
    assert result.stderr == ""
