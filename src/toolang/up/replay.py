"""Prepare a new roaming owner from a retained execution snapshot."""

from pathlib import Path
from contextlib import closing
import shutil
from uuid import uuid4

from toolang.common.layout import AgentLayout
from toolang.driver.chore.store import ChoreStore
from toolang.execution.replay import snapshot_history
from .identity import allocate_roaming
from .workload import fail_preparation
from .source import materialize_roaming_program
from .instance import read_instance, update_instance


def require_complete_seed(layout: AgentLayout) -> None:
    entry = read_instance(layout.runtime_status)
    if entry is not None and entry["control"].get("replay_seed_complete") is False:
        raise ValueError(
            "incomplete replay seed cannot execute or seed another instance"
        )


def prepare_continuation(
    source: AgentLayout,
    *,
    toolang_root: Path,
    source_run: str | None = None,
    source_thread: str | None = None,
) -> AgentLayout:
    if source.source_path is None:
        raise ValueError("retained instance has no source path")
    require_complete_seed(source)
    destination = allocate_roaming(source.source_path, toolang_root=toolang_root)
    try:
        update_instance(
            destination.runtime_status,
            lambda entry: entry["control"].update(replay_seed_complete=False),
        )
        snapshot_history(
            source.run_store,
            destination.run_store,
            source_owner=f"{source.root}:{source.instance_name}",
            source_root=source.root,
            request_id=uuid4().hex,
            source_run=source_run,
            source_thread=source_thread,
        )
        with closing(ChoreStore(destination.run_store)) as chores:
            chores.reset()
        for origin, target in (
            (source.home / ".state", destination.home / ".state"),
            (source.root_state, destination.root_state),
        ):
            if origin.is_dir():
                shutil.copytree(origin, target, dirs_exist_ok=True)
        materialize_roaming_program(source.source_path, layout=destination)
        update_instance(
            destination.runtime_status,
            lambda entry: entry["control"].update(replay_seed_complete=True),
        )
    except BaseException as exc:
        fail_preparation(destination, exc)
        raise
    return destination
