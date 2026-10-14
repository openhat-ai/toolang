from dataclasses import replace
import asyncio

from toolang.common.layout import AgentLayout
from toolang.execution.activity import ActivityQuery
from toolang.execution.store import RunStore
from toolang.up.activity import RuntimeObservation
from toolang.up.observation import RuntimeRegistration, running_instances


def test_all_placements_are_visible_without_talk_or_hub(tmp_path) -> None:
    scope = tmp_path / "scope"
    layouts = [
        AgentLayout(tmp_path / placement, "alice", placement)
        for placement in ("resident", "visiting", "roaming")
    ]
    registrations = []
    try:
        for index, layout in enumerate(layouts):
            store = RunStore(layout.run_store)
            store.create_thread(thread_id=f"script_{index}", origin="script")
            store.close()
            registration = RuntimeRegistration(
                layout, roots=(scope,), drivers=frozenset()
            )
            registration.__enter__()
            registrations.append(registration)
        pages = asyncio.run(RuntimeObservation(scope).read(ActivityQuery()))
        assert {page.placement for page in pages} == {"resident", "visiting", "roaming"}
        assert len({page.agent for page in pages}) == 3
        assert all(page.presence == "online" and page.drivers == () for page in pages)
        assert {thread.id for page in pages for thread in page.threads} == {
            "script_0",
            "script_1",
            "script_2",
        }
    finally:
        for registration in registrations:
            registration.close()
    assert running_instances(scope) == ()


def test_independent_script_instances_have_separate_execution_and_sandbox_state(
    tmp_path,
) -> None:
    source = tmp_path / "app.too"
    one = replace(AgentLayout.roaming(source), instance="one")
    two = replace(one, instance="two")
    assert one.home == two.home
    assert one.run_store != two.run_store
    assert one.chore_store == one.run_store
    assert one.chore_store != two.chore_store
    assert one.runtime_status == two.runtime_status
    store = RunStore(one.run_store)
    store.create_thread(thread_id="talk_existing", origin="talk")
    store.close()
    restarted = RunStore(replace(one).run_store)
    independent = RunStore(two.run_store)
    try:
        assert restarted.get_thread(thread_id="talk_existing") is not None
        assert independent.list_threads() == []
    finally:
        restarted.close()
        independent.close()


def test_stale_registration_and_replaced_owner_are_safe(tmp_path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    old = RuntimeRegistration(layout, roots=(tmp_path,), drivers=frozenset())
    current = RuntimeRegistration(
        layout, roots=(tmp_path,), drivers=frozenset(("chat",))
    )
    old.__enter__()
    current.__enter__()
    old.close()
    assert running_instances(tmp_path)[0]["drivers"] == ["chat"]
    from toolang.up.records import patch_workload

    patch_workload(layout.runtime_status, current.token, {"process_created": 0})
    assert running_instances(tmp_path) == ()
    current.close()


def test_guest_discovery_uses_host_sandbox_ownership_not_guest_pid(
    tmp_path, monkeypatch
):
    from toolang.base.types.sandbox import SandboxRef
    from toolang.up import sandbox
    from toolang.up.records import SandboxState
    from toolang.up.process import write_runtime_state

    layout = AgentLayout.resident(tmp_path, "guest")
    layout.home.mkdir(parents=True)
    alive = True

    class Backend:
        async def running(self, ref):
            return alive

    monkeypatch.setattr(sandbox, "load_state_sandbox", lambda *a: Backend())
    from toolang.plugin.sandboxes.docker import _cli

    monkeypatch.setattr(
        _cli,
        "docker_container_states",
        lambda ids: {identity: alive for identity in ids},
    )
    with RuntimeRegistration(
        layout, roots=(tmp_path,), drivers=frozenset({"chore"})
    ) as registration:
        SandboxState(
            "docker:image",
            SandboxRef("container-one", "http://localhost:1", runtime_kind="container"),
            registration.token,
        ).save(layout.runtime_status)
        write_runtime_state(
            layout,
            endpoint="http://guest:1",
            started_at="now",
            pid=1,
            sandbox="docker:image",
            launch_id=registration.token,
        )
        assert running_instances(tmp_path)[0]["drivers"] == ["chore"]
        alive = False
        # Record changes invalidate the top cache immediately.
        from toolang.up.records import patch_workload

        patch_workload(
            layout.runtime_status, registration.token, {"updated_at": "later"}
        )
        assert running_instances(tmp_path) == ()


def test_unbound_guest_discovery_preserves_unknown_workload(tmp_path):
    from toolang.up.records import patch_workload, update_record

    layout = AgentLayout.resident(tmp_path, "guest")
    with RuntimeRegistration(
        layout, roots=(tmp_path,), drivers=frozenset()
    ) as registration:
        update_record(
            layout.runtime_status,
            lambda entry: entry["control"].update(sandbox="docker:image"),
        )
        patch_workload(
            layout.runtime_status, registration.token, {"process_created": 0}
        )
        before = layout.runtime_status.read_bytes()
        assert running_instances(tmp_path) == ()
        assert layout.runtime_status.read_bytes() == before


def test_short_lived_scripts_share_one_registry_lock_and_readable_references(tmp_path):
    scope = tmp_path / "toolang"
    layout = AgentLayout.roaming(tmp_path / "echo.too")
    directory = scope / ".runtime" / "instances"
    assert running_instances(scope) == ()
    assert not directory.exists()
    for index in range(20):
        with RuntimeRegistration(
            replace(layout, instance=f"script{index}"),
            roots=(scope,),
            drivers=frozenset(),
        ) as registration:
            assert registration.paths[0].name.startswith("echo-roaming-")
            assert registration.paths[0].name.endswith(f"{registration.instance}.json")
    assert tuple(path.name for path in directory.iterdir()) == ("registry.lock",)


def test_registry_guard_keeps_a_new_reference_when_old_cleanup_races(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from toolang.common.files import file_write_lock
    from toolang.up.records import update_record

    layout = AgentLayout.resident(tmp_path, "gus")
    old = RuntimeRegistration(layout, roots=(tmp_path,), drivers=frozenset())
    old.__enter__()
    guard = old.paths[0].parent / "registry.lock"
    current = RuntimeRegistration(
        layout, roots=(tmp_path,), drivers=frozenset(), launch_id="new"
    )
    entered = Event()

    def close_old():
        entered.set()
        old.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_write_lock(guard):
            closing = pool.submit(close_old)
            assert entered.wait(timeout=1)
            assert not closing.done()
            update_record(
                layout.runtime_status, lambda entry: entry.update(launch_id="new")
            )
            current.__enter__()
        closing.result(timeout=1)
    assert current.paths[0].exists()
    current.close()
