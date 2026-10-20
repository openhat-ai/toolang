"""Prepare a new roaming owner from a retained execution snapshot."""

from pathlib import Path
import shutil
from uuid import uuid4

from toolang.common.layout import AgentLayout
from toolang.execution.replay import snapshot_history
from .identity import allocate_roaming, fail_preparation
from .source import materialize_roaming_program


def prepare_continuation(
    source: AgentLayout,
    *,
    toolang_root: Path,
    source_run: str | None = None,
    source_thread: str | None = None,
) -> AgentLayout:
    if source.source_path is None:
        raise ValueError("retained instance has no source path")
    destination = allocate_roaming(source.source_path, toolang_root=toolang_root)
    try:
        snapshot_history(
            source.run_store,
            destination.run_store,
            source_owner=f"{source.root}:{source.instance_name}",
            source_root=source.root,
            request_id=uuid4().hex,
            source_run=source_run,
            source_thread=source_thread,
        )
        for origin, target in (
            (source.home / ".state", destination.home / ".state"),
            (source.root_state, destination.root_state),
        ):
            if origin.is_dir():
                shutil.copytree(origin, target, dirs_exist_ok=True)
        materialize_roaming_program(source.source_path, layout=destination)
    except BaseException as exc:
        fail_preparation(destination, exc)
        raise
    return destination
