from dataclasses import replace
import asyncio
import json

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
    assert one.sandbox_state != two.sandbox_state
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
    payload = dict(current.payload, created=0)
    current.paths[0].write_text(json.dumps(payload))
    assert running_instances(tmp_path) == ()
    current.close()


def test_guest_discovery_uses_host_sandbox_ownership_not_guest_pid(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from toolang.up.process import AgentProcess

    layout = AgentLayout.resident(tmp_path, "guest")
    layout.home.mkdir(parents=True)
    reference = SimpleNamespace(ref=SimpleNamespace(runtime_id="container-one"))
    monkeypatch.setattr(AgentProcess, "reference", property(lambda _: reference))
    monkeypatch.setattr(
        AgentProcess, "status", lambda *a, **k: SimpleNamespace(status="running")
    )
    with RuntimeRegistration(
        layout,
        roots=(tmp_path,),
        drivers=frozenset({"chore"}),
        sandbox_runtime="container-one",
    ) as registration:
        registration.payload["created"] = 0
        registration.paths[0].write_text(json.dumps(registration.payload))
        assert running_instances(tmp_path)[0]["drivers"] == ["chore"]
        reference.ref.runtime_id = "container-two"
        assert running_instances(tmp_path) == ()
