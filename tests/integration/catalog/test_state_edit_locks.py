"""Cross-process exclusion and lazy reads for authored State scopes."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import multiprocessing
from pathlib import Path
from threading import Event, current_thread

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
from toolang.up.source import materialize_roaming_program


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
        AuthoredChores(
            layout.home,
            lock_path=layout.home_edit_lock,
            agent_name=layout.name,
        ).create(ChoreFile.parse("New.", name="new"))
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
    assert (
        AuthoredChores(
            layout.home,
            lock_path=layout.home_edit_lock,
            agent_name=layout.name,
        ).list()
        == ()
    )
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


def test_delayed_roaming_projection_does_not_overwrite_newer_workspace_config(
    tmp_path, monkeypatch
):
    source = tmp_path / "alice.too"
    source.write_text("agic:\n  Hello.\n")
    layout = materialize_roaming_program(source)
    grants = ConfiguredWorkspaces(
        tmp_path / "toolang.toml", lock_path=layout.root_edit_lock
    )
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
    grants.add(tmp_path / "first", name="first")
    paused, resume = Event(), Event()
    original = EditLock.write

    @contextmanager
    def write(lock):
        if (
            lock.path == layout.home_edit_lock
            and current_thread().name.startswith("old-projection")
            and not paused.is_set()
        ):
            paused.set()
            assert resume.wait(10)
        with original(lock):
            yield

    monkeypatch.setattr(EditLock, "write", write)
    with ThreadPoolExecutor(thread_name_prefix="old-projection") as pool:
        pending = pool.submit(materialize_roaming_program, source)
        try:
            assert paused.wait(10)
            grants.add(tmp_path / "second", name="second")
            materialize_roaming_program(source)
        finally:
            resume.set()
        pending.result(timeout=10)
    projected = ConfiguredWorkspaces(layout.config, lock_path=layout.home_edit_lock)
    assert set(projected.list()) == {"first", "second"}


def test_effective_chores_retry_inline_and_file_reads_after_first_edit(
    tmp_path, monkeypatch
):
    from toolang.catalog import chores

    layout = AgentLayout.resident(tmp_path, "alice")
    directory = layout.home / "chores"
    directory.mkdir(parents=True)
    layout.program.write_text("chore first:\n  Old inline.\n")
    definition = directory / "second.md"
    definition.write_text("Old file.")
    original = chores.load_chore_files
    changed = False

    def read_files(layout):
        nonlocal changed
        if not changed:
            changed = True
            with EditLock(layout.home_edit_lock).write():
                atomic_write_text(layout.program, "chore first:\n  New inline.\n")
                atomic_write_text(definition, "New file.")
        return original(layout)

    monkeypatch.setattr(chores, "load_chore_files", read_files)
    assert {item.name: item.body for item in chores.load_chores(layout)} == {
        "first": "New inline.",
        "second": "New file.",
    }


def test_chore_inspection_retries_when_valid_source_becomes_invalid(
    tmp_path, monkeypatch
):
    from toolang.catalog.chores import inspect_chore_sources

    layout = AgentLayout.resident(tmp_path, "alice")
    directory = layout.home / "chores"
    directory.mkdir(parents=True)
    definition = directory / "review.md"
    definition.write_text("Valid file.")
    original = AuthoredChores.sources
    calls = 0

    def sources(catalog):
        nonlocal calls
        calls += 1
        if calls == 2:
            with EditLock(layout.home_edit_lock).write():
                atomic_write_text(
                    definition, "---\nschedule: invalid\n---\nInvalid file."
                )
        return original(catalog)

    monkeypatch.setattr(AuthoredChores, "sources", sources)
    inspected = inspect_chore_sources(layout)
    assert len(inspected) == 1
    assert inspected[0].source_error is not None


def test_concurrent_chore_api_patches_preserve_independent_fields(
    tmp_path, monkeypatch
):
    import asyncio

    from toolang.api.routers.chores import _update_chore
    from toolang.api.schemas import ChorePatchRequest
    from toolang.catalog.manager import ChoresManager
    from toolang.up import AgentCore

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    manager = ChoresManager(layout)
    manager.home_authoring.create(ChoreFile.parse("Original.", name="review"))
    core = AgentCore(layout, drivers=frozenset())
    paused, resume, second_progress = Event(), Event(), Event()
    original_update = AuthoredChores.update
    original_lock = files.file_write_lock

    def update(catalog, entry):
        if current_thread().name.startswith("title-patch"):
            paused.set()
            assert resume.wait(10)
        saved = original_update(catalog, entry)
        if current_thread().name.startswith("schedule-patch"):
            second_progress.set()
        return saved

    @contextmanager
    def acquire(path, **kwargs):
        if current_thread().name.startswith("schedule-patch"):
            kwargs["on_wait"] = second_progress.set
        with original_lock(path, **kwargs):
            yield

    monkeypatch.setattr(AuthoredChores, "update", update)
    monkeypatch.setattr(files, "file_write_lock", acquire)
    try:
        with ThreadPoolExecutor(thread_name_prefix="title-patch") as first_pool:
            first = first_pool.submit(
                _update_chore,
                core,
                manager,
                "review",
                ChorePatchRequest(title="Updated title"),
            )
            try:
                assert paused.wait(10)
                with ThreadPoolExecutor(
                    thread_name_prefix="schedule-patch"
                ) as second_pool:
                    second = second_pool.submit(
                        _update_chore,
                        core,
                        manager,
                        "review",
                        ChorePatchRequest(schedule="FREQ=DAILY"),
                    )
                    try:
                        assert second_progress.wait(10)
                    finally:
                        resume.set()
                    first.result(timeout=10)
                    second.result(timeout=10)
            finally:
                resume.set()
        saved = manager.home_authoring.get("review")
        assert saved is not None
        assert saved.title == "Updated title"
        assert saved.schedule == "FREQ=DAILY"
    finally:
        asyncio.run(core.close())


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
