"""Opt-in chat TUI system tests backed by a real model provider.

Run the DeepSeek cases explicitly with:

    uv run pytest -q tests/system/cli/test_chat_tui_live_e2e.py \
      -m live_provider \
      --live-model 'deepseek/deepseek-chat[deepseek]'
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.execution.store import RunStore
from tests.support.chat_tui_pty import ChatTuiPtySession
from tests.support.live_provider import LIVE_RESPONSE_PREFIX

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.name != "posix",
        reason="pseudo-terminal chat testing requires POSIX",
    ),
]


@pytest.fixture
def deepseek_model(request: pytest.FixtureRequest) -> str:
    """Return an explicitly selected DeepSeek model or skip these cases."""

    value = request.config.getoption("--live-model")
    if not isinstance(value, str) or not value.strip():
        pytest.skip("pass --live-model to run real-provider chat tests")
    model = value.strip()
    if "deepseek" not in model.lower():
        pytest.skip("chat TUI live E2E cases require a DeepSeek model query")
    return model


@pytest.mark.parametrize(
    ("kind", "marker", "progress"),
    [
        pytest.param("agic", "TOOLANG_CHAT_AGIC_E2E", None, id="agic"),
        pytest.param("flow", "TOOLANG_CHAT_FLOW_E2E", "[0] Run smoke", id="flow"),
    ],
)
def test_chat_tui_runs_with_real_deepseek_provider(
    tmp_path: Path,
    deepseek_model: str,
    kind: str,
    marker: str,
    progress: str | None,
) -> None:
    session = ChatTuiPtySession.start(
        "tests.support.chat_tui_live_e2e",
        tmp_path,
        deepseek_model,
        kind,
    )
    try:
        session.wait_for("Toolang", "^d exit", timeout=30)
        session.send(marker.encode())
        session.wait_for(marker)
        session.send(b"\r")
        response = f"{LIVE_RESPONSE_PREFIX} {marker}"
        expected = [f"> {marker}", response, "succeeded"]
        if progress is not None:
            expected.append(progress)
        output = session.wait_for(*expected, timeout=180)

        assert "run_" in output
        assert "Traceback" not in output

        if kind == "flow":
            session.send(b"/output\r")
            session.wait_for(response, timeout=30)

        session.send(b"\x04")
        return_code = session.wait_for_exit(timeout=30)
        assert return_code == 0, session.output

        store = RunStore(AgentLayout.resident(tmp_path, "alice").run_store)
        try:
            root_runs = [
                run for run in store.list_runs(limit=None) if run.parent is None
            ]
            assert len(root_runs) == 1
            assert store.run_output_text(run_id=root_runs[0].id) == response
        finally:
            store.close()
    finally:
        session.close()
