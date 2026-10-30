from pathlib import Path
import asyncio

from toolang.common.watching import directory_changes
from toolang.common import watching


def test_shallow_watch_discovers_missing_directories_and_keeps_periodic_checks(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "nested"
    target = nested / "new.too"
    native = watching.awatch

    def directories():
        return (source, nested) if nested.is_dir() else (source,)

    async def scenario():
        stop = asyncio.Event()
        subscribed = asyncio.Event()

        async def observe(*paths, **kwargs):
            assert kwargs["recursive"] is False
            if nested in paths:
                subscribed.set()
            async for batch in native(*paths, **kwargs):
                yield batch

        monkeypatch.setattr(watching, "awatch", observe)
        changes = directory_changes(directories, stop_signal=stop, interval_ms=50)
        assert await anext(changes) == frozenset()

        async def received():
            async for paths in changes:
                if target in paths:
                    return

        reader = asyncio.create_task(received())
        try:
            nested.mkdir()
            async with asyncio.timeout(10):
                await subscribed.wait()
                target.write_text("new")
                await reader
        finally:
            stop.set()
            reader.cancel()
            await changes.aclose()

    asyncio.run(scenario())


def test_periodic_check_is_not_starved_by_filtered_native_events(
    tmp_path: Path, monkeypatch
):
    async def scenario():
        stop = asyncio.Event()
        native_batches = 0

        async def filtered_watch(*_args, **_kwargs):
            nonlocal native_batches
            # Native filtering can keep awatch busy without yielding a batch.
            while not stop.is_set():
                native_batches += 1
                await asyncio.sleep(0.001)
            if False:
                yield set()

        monkeypatch.setattr(watching, "awatch", filtered_watch)
        changes = directory_changes(
            lambda: (tmp_path,), stop_signal=stop, interval_ms=50
        )
        try:
            assert await anext(changes) == frozenset()
            async with asyncio.timeout(10):
                assert await anext(changes) == frozenset()
                assert await anext(changes) == frozenset()
            assert native_batches > 0
        finally:
            stop.set()
            await changes.aclose()

    asyncio.run(scenario())
