"""Top's complete statistical roster is independent of runtime liveness."""

from dataclasses import replace
import shutil

import pytest

from tests.support.runtime_observation import binding
from toolang.common.layout import AgentLayout
from toolang.up import scope
from toolang.up.observation import RuntimeInventory
from toolang.up.scope import ScopeReader
from toolang.up.types import RuntimeInstanceSnapshot


def inventory(*sources, diagnostics=()):
    return RuntimeInventory(
        RuntimeInstanceSnapshot(
            tuple(source.instance for source in sources), diagnostics
        ),
        tuple(sources),
    )


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
def test_scope_reconciles_registration_lifecycle_by_placement(
    tmp_path, monkeypatch, placement
):
    source = binding(tmp_path)
    source = replace(
        source,
        instance=replace(
            source.instance,
            identity="alice" if placement == "resident" else "worker",
            placement=placement,
        ),
    )
    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    current = [inventory(source)]
    monkeypatch.setattr(scope, "runtime_inventory", lambda *args, **kwargs: current[0])
    reader = ScopeReader(tmp_path, agent=None)
    first = reader.read()
    assert len(first.sources) == (1 if placement == "resident" else 2)
    assert source in first.sources
    # A new launch replaces one binding, never duplicates its logical history.
    newer = replace(source, launch_id="replacement")
    current[0] = inventory(newer)
    assert newer in reader.read().sources
    current[0] = inventory()
    stopped = reader.read()
    assert len(stopped.sources) == 1
    assert stopped.sources[0].instance.placement == "resident"
    assert stopped.sources[0].endpoint is None
    assert stopped.sources[0].registration is None
    assert stopped == ScopeReader(tmp_path, agent=None).read()
    shutil.rmtree(resident.home)
    assert reader.read().sources == ()


def test_scope_keeps_registered_resident_even_when_home_is_missing(
    tmp_path, monkeypatch
):
    source = binding(tmp_path)
    source = replace(
        source,
        instance=replace(
            source.instance,
            identity="alice",
            state="unknown",
            diagnostic="home unreadable",
        ),
    )
    monkeypatch.setattr(
        scope, "runtime_inventory", lambda *args, **kwargs: inventory(source)
    )
    result = ScopeReader(tmp_path, agent=None).read()
    assert result.sources == (source,)
    assert result.snapshot.partial


@pytest.mark.parametrize("failure", ["homes", "registry", "registry_exception"])
def test_enumeration_failure_preserves_unknown_coverage_until_recovery(
    tmp_path, monkeypatch, failure
):
    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    source = binding(tmp_path)
    source = replace(source, instance=replace(source.instance, placement="roaming"))
    current = [inventory(source)]
    monkeypatch.setattr(scope, "runtime_inventory", lambda *args, **kwargs: current[0])
    reader = ScopeReader(tmp_path, agent=None)
    assert len(reader.read().sources) == 2
    shutil.rmtree(resident.home)
    current[0] = inventory()
    with monkeypatch.context() as patch:

        def denied(*args, **kwargs):
            raise PermissionError("permission denied")

        if failure == "homes":
            patch.setattr(scope, "agent_layouts", denied)
        elif failure == "registry_exception":
            patch.setattr(scope, "runtime_inventory", denied)
        else:
            current[0] = inventory(diagnostics=("registry unreadable",))
        result = reader.read()
        assert result.snapshot.diagnostics
        assert len(result.sources) == 1
        assert result.sources[0].instance.placement == (
            "resident" if failure == "homes" else "roaming"
        )
    current[0] = inventory()
    assert reader.read().sources == ()


@pytest.mark.parametrize("agent", [None, "alice"])
def test_unreadable_home_metadata_is_unknown_instead_of_absent(
    tmp_path, monkeypatch, agent
):
    resident = AgentLayout.resident(tmp_path, "alice")
    resident.home.mkdir(parents=True)
    original = type(tmp_path).stat

    def denied(path, *args, **kwargs):
        if path == resident.home:
            raise PermissionError("home unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "stat", denied)
    result = ScopeReader(tmp_path, agent=agent).read()
    assert (
        result.snapshot.diagnostics
        and "home unreadable" in result.snapshot.diagnostics[0]
    )


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
def test_scope_observation_keeps_registered_stopped_instances_without_mutation(
    tmp_path, placement
):
    from toolang.up.identity import allocate_roaming
    from toolang.up.instance import update_instance
    from toolang.up.workload import ForegroundWorkload

    layout = (
        AgentLayout.resident(tmp_path, "alice")
        if placement == "resident"
        else AgentLayout.visiting(
            "https://example.com/alice.too", "alice", toolang_root=tmp_path
        )
        if placement == "visiting"
        else allocate_roaming(tmp_path / "alice.too", toolang_root=tmp_path)
    )
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as workload:
        update_instance(
            layout.runtime_status,
            lambda entry: entry["workload"].update(status="stopped", pid=None),
            instance=layout.instance,
            launch_id=workload.token,
        )
        reference, status = (
            workload.path.read_bytes(),
            layout.runtime_status.read_bytes(),
        )
        snapshot = ScopeReader(tmp_path, agent=None).read()
        assert workload.path.exists(), "observation removed the registration"
        assert workload.path.read_bytes() == reference
        assert layout.runtime_status.read_bytes() == status
        assert len(snapshot.sources) == 1
        assert snapshot.sources[0].instance.state == "not running"


def test_scope_probes_dead_process_without_repairing_runtime_state(
    tmp_path, monkeypatch
):
    from toolang.up import process
    from toolang.up.workload import ForegroundWorkload

    layout = AgentLayout.resident(tmp_path, "alice")
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as workload:
        before = layout.runtime_status.read_bytes()
        reference = workload.path.read_bytes()

        def forbidden(*args, **kwargs):
            raise AssertionError("observation attempted runtime repair")

        monkeypatch.setattr(process, "process_alive", lambda *args: False)
        monkeypatch.setattr(process, "recover_control_write_lock", forbidden)
        monkeypatch.setattr(process.AgentProcess, "reconcile", forbidden)
        result = ScopeReader(tmp_path, agent=None).read()
        assert len(result.sources) == 1
        assert result.sources[0].instance.state == "not running"
        assert layout.runtime_status.read_bytes() == before
        assert workload.path.read_bytes() == reference
