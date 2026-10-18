"""Rendering tests must not depend on the caller's terminal type."""

import os
import subprocess
import sys

import pytest

from tests import PROJECT_ROOT


@pytest.mark.parametrize(
    ("term", "workers"),
    [(None, 0), ("dumb", 0), ("unknown", 0), ("xterm-256color", 0), ("dumb", 2)],
)
def test_hub_rendering_is_independent_of_inherited_term(term, workers):
    env = dict(os.environ)
    if term is None:
        env.pop("TERM", None)
    else:
        env["TERM"] = term
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "tests/unit/cli/test_hub_status.py",
    ]
    if workers:
        command.extend(["-n", str(workers)])

    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
