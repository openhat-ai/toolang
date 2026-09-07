"""Compact admission is cross-process and cancellation releases no other's lock."""

import asyncio
import sys

from toolang.execution.executor.compact import permit


def test_permit_excludes_another_process_and_releases_after_cancellation(tmp_path):
    path = tmp_path / "compact.lock"

    async def scenario():
        async with permit(path):
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import fcntl, sys; f=open(sys.argv[1], 'a+b'); "
                "print('waiting', flush=True); fcntl.flock(f, fcntl.LOCK_EX); "
                "print('acquired', flush=True)",
                str(path),
                stdout=asyncio.subprocess.PIPE,
            )
            assert child.stdout is not None
            assert await asyncio.wait_for(child.stdout.readline(), 2) == b"waiting\n"
            waiting = asyncio.create_task(child.stdout.readline())
            done, _pending = await asyncio.wait({waiting}, timeout=0.05)
            assert not done

            async def waiter():
                async with permit(path):
                    raise AssertionError("another caller owns the permit")

            canceled = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            canceled.cancel()
            try:
                await canceled
            except asyncio.CancelledError:
                pass
            assert not waiting.done()
        assert await asyncio.wait_for(waiting, 2) == b"acquired\n"
        assert await asyncio.wait_for(child.wait(), 2) == 0
        async with permit(path):
            pass

    asyncio.run(scenario())
