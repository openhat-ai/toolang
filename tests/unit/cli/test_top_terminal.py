"""Exercise the real prompt-toolkit bindings without a provider or terminal."""

import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from toolang.cli.common.top import top_application
from toolang.cli.common.top_view import TopView
from toolang.up.schemas import TopSnapshot
from toolang.up.top import TopObserver
from toolang.up.types import RuntimeInstanceSnapshot


@pytest.mark.parametrize("exit_key", ["\x1b[21~", "q", "\x03"])
def test_function_keys_aliases_and_application_exit(tmp_path, monkeypatch, exit_key):
    observer = TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    snapshot = TopSnapshot(
        observed_at="2026-10-18T12:34:37Z",
        period=3600,
        recent=300,
        inventory=RuntimeInstanceSnapshot(()),
        sources=(),
    )
    monkeypatch.setattr(
        observer,
        "tick",
        lambda: snapshot.model_copy(
            update={"period": observer.period, "recent": observer.recent}
        ),
    )
    view = TopView()
    keys = iter(
        ("\x1bOR", "\r", "\x1b[15~", "\x1b[18~", "\x1b[19~", "\x1bOP", exit_key)
    )
    states = []

    async def scenario():
        with create_pipe_input() as pipe:
            app = top_application(observer, view, input=pipe, output=DummyOutput())

            def rendered(_):
                states.append((view.details, view.list_mode, view.help))
                key = next(keys, None)
                if key is not None:
                    pipe.send_text(key)

            app.after_render += rendered
            await asyncio.wait_for(app.run_async(), timeout=5)
            assert not app.is_running

    try:
        asyncio.run(scenario())
        assert any(details for details, _, _ in states)
        assert view.list_mode and view.help and not view.details
        assert observer.recent == 900 and observer.period == 14400
    finally:
        observer.close()
