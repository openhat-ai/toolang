from __future__ import annotations

import pytest

from toolang.base.types.model import ModelOverride
from toolang.cli.toolang.commands import thread


def test_rerun_model_option_uses_the_shared_model_body() -> None:
    assert thread._rerun_model_override(
        model_body="test/model effort=4096",
    ) == ModelOverride(identity="test/model", effort=4096)
    assert thread._rerun_model_override(model_body="effort=high") == ModelOverride(
        effort="high"
    )
    assert thread._rerun_model_override(model_body=None) is None


def test_rerun_model_option_rejects_model_removal() -> None:
    with pytest.raises(ValueError, match="does not accept unset"):
        thread._rerun_model_override(model_body="unset")
    with pytest.raises(ValueError, match="was removed"):
        thread._rerun_model_override(model_body="none")


@pytest.mark.parametrize("kind", ["retry", "rerun"])
@pytest.mark.parametrize("show_progress", [False, True])
def test_restart_uses_shared_terminal_surfaces(
    tmp_path, monkeypatch, kind, show_progress
):
    import asyncio
    from contextlib import asynccontextmanager
    from io import StringIO

    from toolang.cli.common.terminal_surfaces import LIGHT_TERMINAL_SURFACES
    from toolang.common.layout import AgentLayout

    class ReachedRun(Exception):
        pass

    environ = {"TOOLANG_COLOR_SCHEME": "light"}
    stream = StringIO()
    monkeypatch.setattr(thread.sys, "stderr", stream)
    monkeypatch.setattr(thread, "load_runtime_environ", lambda *args, **kwargs: environ)
    resolutions = []
    original = thread.resolve_terminal_surfaces

    def resolve(**kwargs):
        assert kwargs == {"environment": environ, "output_stream": stream}
        resolutions.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(thread, "resolve_terminal_surfaces", resolve)

    class Client:
        async def retry(self, request, *, tracer):
            assert len(resolutions) == int(show_progress)
            if show_progress:
                assert tracer.console.surfaces == LIGHT_TERMINAL_SURFACES
            else:
                assert tracer is None
            raise ReachedRun

        rerun = retry

    @asynccontextmanager
    async def acquire(*args, **kwargs):
        yield Client()

    monkeypatch.setattr(thread, "acquire_run_client", acquire)
    with pytest.raises(ReachedRun):
        asyncio.run(
            thread._execute_retry_or_rerun(
                layout=AgentLayout.resident(tmp_path, "alice"),
                server=None,
                kind=kind,
                source="run_source",
                anchor=None,
                commands=(),
                model_override=None,
                show_progress=show_progress,
                model_catalog=None,
            )
        )
