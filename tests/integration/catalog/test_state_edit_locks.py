"""Cross-process exclusion and lazy reads for authored State scopes."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from hashlib import sha256
import multiprocessing
from pathlib import Path

import pytest

from toolang.catalog.cap import CapFile
from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog.config import CapRef
from toolang.catalog.manager import CapsManager
from toolang.common import files
from toolang.common.files import EditLock, atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.execution.tools.state.handlers import execute
from toolang.execution.tools.state.schemas import ResourceRequest
from toolang.execution.tools.state.types import StateToolContext
from toolang.state.config import ConfiguredWorkspaces
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateWatcher
from toolang.up.process import materialize_roaming_program


def _first_writer(lock_path, target, start, written):
    assert start.wait(10)
    with EditLock(Path(lock_path)).write():
        atomic_write_text(Path(target), "after")
        written.set()


@pytest.mark.parametrize("failure", ["none", "io", "wrapped"])
def test_read_retries_when_first_writer_creates_guard(tmp_path, failure):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("before")
    ctx = multiprocessing.get_context("spawn")
    start, written = ctx.Event(), ctx.Event()
    process = ctx.Process(
        target=_first_writer,
        args=(str(layout.home_edit_lock), str(layout.program), start, written),
    )
    process.start()
    attempts = []

    def read():
        content = layout.program.read_text()
        attempts.append(content)
        if len(attempts) == 1:
            start.set()
            assert written.wait(10)
            if failure == "wrapped":
                try:
                    raise FileNotFoundError("removed configuration")
                except OSError as exc:
                    raise ValueError("could not read configuration") from exc
            if failure == "io":
                raise FileNotFoundError("removed during enumeration")
        return content

    try:
        assert EditLock(layout.home_edit_lock).read(read) == "after"
        assert attempts == ["before", "after"]
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def _mutate(root, family, contended, done):
    layout = AgentLayout.resident(Path(root), "alice")
    original = files.fcntl.flock

    def flock(fd, flags):
        try:
            return original(fd, flags)
        except BlockingIOError:
            contended.set()
            raise

    pytest.MonkeyPatch().setattr(files.fcntl, "flock", flock)
    if family == "cap":
        CapsManager(layout).home_authoring.create(
            CapFile.parse("New.", kind="prompt", name="new")
        )
    elif family == "config":
        CapsManager(layout).home_configured.create(
            CapRef(kind="prompt", name="new", ref="https://example.test/new.md")
        )
    elif family == "chore":
        AuthoredChores(layout.home, lock_path=layout.home_edit_lock).create(
            ChoreFile.parse("New.", name="new")
        )
    elif family == "workspace":
        ConfiguredWorkspaces(layout.config, lock_path=layout.home_edit_lock).add(
            layout.home, name="repo"
        )
    elif family == "state":
        context = StateToolContext(
            home=layout.home, room=layout.tool_room("state"), layout=layout
        )
        execute(
            ResourceRequest(
                "create", key="runnables/new.too", content="flow:\n  pass\n"
            ),
            context,
        )
    elif family == "projection":
        source = layout.root.parent / "alice.too"
        materialize_roaming_program(source)
    else:
        other = AgentLayout.resident(layout.root, family)
        with EditLock(other.home_edit_lock).write():
            pass
    done.set()


@pytest.mark.parametrize(
    "family", ["cap", "config", "chore", "workspace", "state", "projection"]
)
def test_home_writers_share_edit_lock_across_processes(tmp_path, family):
    source = tmp_path / "alice.too"
    source.write_text("agic:\n  Hello.\n")
    layout = AgentLayout.resident(tmp_path / ".toolang", "alice")
    layout.home.mkdir(parents=True)
    ctx = multiprocessing.get_context("spawn")
    contended, done = ctx.Event(), ctx.Event()
    process = ctx.Process(
        target=_mutate, args=(str(layout.root), family, contended, done)
    )
    try:
        with EditLock(layout.home_edit_lock).write():
            process.start()
            assert contended.wait(10), family
            assert not done.is_set()
        assert done.wait(10), family
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    assert not tuple(layout.home.glob("*.lock"))


def test_other_home_and_root_edits_remain_independent(tmp_path):
    alice = AgentLayout.resident(tmp_path, "alice")
    bob = AgentLayout.resident(tmp_path, "bob")
    alice.home.mkdir(parents=True)
    bob.home.mkdir(parents=True)
    with EditLock(alice.home_edit_lock).write():
        with ThreadPoolExecutor() as pool:

            def edit():
                with EditLock(bob.home_edit_lock).write():
                    with EditLock(alice.root_edit_lock).write():
                        return True

            assert pool.submit(edit).result(timeout=10)


def test_catalog_reads_and_unchanged_projection_do_not_create_guards(tmp_path):
    from toolang.common.config_sources import project_sources, script_sources

    source = tmp_path / "alice.too"
    source.write_text("agic:\n  Hello.\n")
    layout = AgentLayout.roaming(source)
    layout.home.mkdir(parents=True)
    layout.config.write_text(
        project_sources(script_sources(source), source=source, catalog=None)
    )
    caps = CapsManager(layout)
    assert caps.root_authoring.list() == caps.home_authoring.list() == ()
    assert caps.root_configured.list() == caps.home_configured.list() == ()
    assert AuthoredChores(layout.home, lock_path=layout.home_edit_lock).list() == ()
    assert (
        ConfiguredWorkspaces(layout.config, lock_path=layout.home_edit_lock).list()
        == {}
    )
    assert materialize_roaming_program(source) == layout
    assert not (layout.home / ".state").exists()
    assert not (layout.root / ".state").exists()


def _add_roaming_workspace(source, name):
    from toolang.cli.toolang.commands.workspace import (
        _authored_config,
        _workspace_edit_lock,
    )

    layout = AgentLayout.roaming(Path(source))
    assert _workspace_edit_lock(layout) == layout.root_edit_lock
    ConfiguredWorkspaces(
        _authored_config(layout), lock_path=_workspace_edit_lock(layout)
    ).add(Path(source).parent / name, name=name)


def test_shared_roaming_config_updates_preserve_both_scripts_workspaces(tmp_path):
    sources = [tmp_path / f"{name}.too" for name in ("alice", "bob")]
    for source in sources:
        source.write_text("agic:\n  Hello.\n")
        (tmp_path / source.stem).mkdir()
        materialize_roaming_program(source)
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        tuple(pool.map(_add_roaming_workspace, map(str, sources), ["alice", "bob"]))
    layout = AgentLayout.roaming(sources[0])
    workspaces = ConfiguredWorkspaces(
        tmp_path / "toolang.toml", lock_path=layout.root_edit_lock
    ).list()
    assert set(workspaces) == {"alice", "bob"}
    assert layout.root_edit_lock.is_file()
    assert not (tmp_path / "toolang.toml.lock").exists()


def test_watcher_ignores_edit_lock_and_lock_activity_keeps_revision(tmp_path):
    import asyncio

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agic:\n  Hello.\n")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout, initial_state=initial)
    with EditLock(layout.home_edit_lock).write():
        with ThreadPoolExecutor() as pool:
            refreshed = pool.submit(lambda: asyncio.run(watcher.refresh())).result(
                timeout=10
            )
    assert refreshed.revision == initial.revision
    assert (layout.root_state / "build.lock").is_file()
    assert (layout.home_state / "build.lock").is_file()
    assert (layout.agent_state / "publish.lock").is_file()
    assert not tuple(layout.root.rglob("prepare.lock"))
    assert not tuple(layout.root.rglob("check.lock"))


def test_legacy_sidecars_are_untouched_and_root_stores_share_lock(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    legacy = [
        layout.home / name
        for name in (
            "caps.lock",
            "chores.lock",
            "agent.too.lock",
            "config.toml.lock",
            "runnables.lock",
        )
    ]
    for path in legacy:
        path.write_text("legacy")
    caps = CapsManager(layout)
    assert (
        caps.root_authoring.lock_path
        == caps.root_configured.lock_path
        == layout.root_edit_lock
    )
    with caps.root_authoring.write_lock():
        caps.root_configured.create(
            CapRef(kind="prompt", name="new", ref="https://example.test/new.md")
        )
    assert not layout.home_edit_lock.exists()
    context = StateToolContext(
        home=layout.home, room=layout.tool_room("state"), layout=layout
    )
    execute(
        ResourceRequest("create", key="agent.too", content="agic:\n  Hello.\n"), context
    )
    digest = sha256(layout.program.read_bytes()).hexdigest()
    assert execute(ResourceRequest("get", key="agent.too"), context)["digest"] == digest
    assert [path.read_text() for path in legacy] == ["legacy"] * len(legacy)
