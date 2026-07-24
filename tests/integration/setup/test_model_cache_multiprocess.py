from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import multiprocessing
from pathlib import Path
import time
from typing import Any

from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.setup import ModelListCache


@dataclass(frozen=True)
class _SlowProvider:
    calls_path: Path
    name: str = "shared"
    description: str | None = None

    def required_env_vars(self) -> tuple[str, ...]:
        return ()

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return "https://models.example.test/v1"

    def default_api_key_env(self) -> str | None:
        return None

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        with self.calls_path.open("a", encoding="utf-8") as stream:
            stream.write("called\n")
        time.sleep(0.1)
        return (
            ModelInfo(
                ref="shared/one",
                provider=self.name,
                name="one",
                model="one",
            ),
        )

    def prepare_target(self, target: ModelTarget) -> ModelTarget:
        return target


def _refresh(cache_dir: str, calls_path: str, start: Any) -> None:
    start.wait()
    asyncio.run(
        ModelListCache(Path(cache_dir)).get(
            _SlowProvider(Path(calls_path)),
            envs={},
            refresh=True,
        )
    )


def test_concurrent_force_refresh_is_coalesced_across_processes(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    cache_dir = tmp_path / "models"
    calls_path = tmp_path / "calls.txt"
    processes = [
        context.Process(
            target=_refresh,
            args=(str(cache_dir), str(calls_path), start),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert calls_path.read_text(encoding="utf-8").splitlines() == ["called"]
    record = ModelListCache(cache_dir).read(_SlowProvider(calls_path), envs={})
    assert record is not None
    assert record.generation == 1
    assert tuple(model.ref for model in record.models) == ("shared/one",)
