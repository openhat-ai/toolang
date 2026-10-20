import time

import pytest

from toolang.cli.toolang.main import main
from toolang.execution.store import RunStore
from toolang.stats.recent import recent_source
from toolang.up.identity import select_roaming


def test_roaming_chat_help_does_not_allocate_an_instance(tmp_path, monkeypatch):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "global"))
    assert main([str(source), "chat", "--help"]) == 0
    assert not list((tmp_path / ".toolang").glob("aid@r*"))


def test_roaming_argument_failure_finalizes_the_published_identity(
    tmp_path, monkeypatch
):
    from toolang.up.instance import read_instance

    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    assert main([str(source), "chat", "--unknown"]) == 2
    layout = select_roaming(source)
    entry = read_instance(layout.runtime_status)
    assert entry is not None and entry["workload"]["status"] == "failed"
    assert not list((root / ".runtime" / "instances").glob("*.json"))


def test_interrupted_script_preparation_keeps_failed_history_without_registration(
    tmp_path, monkeypatch
):
    from toolang.up import source as sources
    from toolang.up.instance import read_instance

    source = tmp_path / "aid.too"
    source.write_text("flow main(_: Text):\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(sources, "materialize_roaming_program", interrupt)
    assert main([str(source), "main", "--quiet", "hello"]) == 130
    entry = read_instance(select_roaming(source).runtime_status)
    assert entry is not None and entry["workload"]["status"] == "failed"
    assert not list((root / ".runtime" / "instances").glob("*.json"))


def test_resident_stop_selects_and_fences_the_original_owner(tmp_path, monkeypatch):
    from toolang.common.layout import AgentLayout
    from toolang.up.instance import read_instance, update_instance
    from toolang.up.process import AgentProcess

    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    layout = AgentLayout.resident(root, "aid")
    layout.home.mkdir(parents=True, exist_ok=True)
    update_instance(
        layout.runtime_status, lambda entry: entry.update(launch_id="selected-owner")
    )
    entry = read_instance(layout.runtime_status)
    assert entry is not None and entry["launch_id"] == "selected-owner"
    captured = {}

    async def stop(self, **kwargs):
        captured.update(layout=self.layout, **kwargs)
        return True

    monkeypatch.setattr(AgentProcess, "stop", stop)
    assert main(["aid", "stop"]) == 0
    assert captured["layout"].root == layout.root
    assert captured["expected_launch_id"] == "selected-owner"


@pytest.mark.parametrize("command", ["serve", "start", "stop"])
@pytest.mark.parametrize("target_first", [True, False])
def test_roaming_rejects_hosting_commands_without_mutating_instances(
    tmp_path, monkeypatch, command, target_first
):
    from toolang.up.identity import allocate_roaming

    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    layout = allocate_roaming(source, toolang_root=root)
    assert layout.instance_id is not None
    before = layout.runtime_status.read_bytes()
    references = {
        path.name: path.read_bytes()
        for path in (root / ".runtime" / "instances").glob("*.json")
    }
    args = [str(source), command] if target_first else [command, str(source)]
    assert main([*args, "-i", layout.instance_id]) != 0
    assert layout.runtime_status.read_bytes() == before
    assert list((source.parent / ".toolang").glob("aid@r*")) == [layout.root]
    assert {
        path.name: path.read_bytes()
        for path in (root / ".runtime" / "instances").glob("*.json")
    } == references


def test_retained_selection_offline_edits_and_isolated_replay(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "aid.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "global"))
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    first = select_roaming(source)
    assert first.instance_id is not None
    with_store = RunStore(first.run_store, read_only=True)
    run = with_store.list_runs()[0]
    with_store.close()
    assert main([str(source), "main", "--quiet", "again"]) == 0
    second = select_roaming(source)
    assert first.root != second.root
    assert main([str(source), "inspect", "-i", first.instance_id, "runs"]) == 0
    assert run.id in capsys.readouterr().out
    from toolang.cli.toolang.commands import thread

    with monkeypatch.context() as scope:
        scope.setattr(
            thread,
            "acquire_agent_server",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("offline edit started an executor")
            ),
        )
        assert main([str(source), "fork", "-i", first.instance_id, run.id]) == 0
    assert main([str(source), "rerun", "-i", first.instance_id, run.id]) == 0
    destination = select_roaming(source)
    assert destination.root not in {first.root, second.root}
    copied = RunStore(destination.run_store, read_only=True)
    try:
        runs = copied.list_runs()
        assert len(runs) == 2
        assert sum(copied.is_imported(item.id) for item in runs) == 1
        assert all(item.status == "succeeded" for item in runs)
        receipt = copied._conn.execute(
            "SELECT destination_run FROM replay_provenance WHERE source_run=?",
            (run.id,),
        ).fetchone()
        assert receipt is not None and receipt[0] != run.id
    finally:
        copied.close()
    with recent_source(destination.run_store) as reader:
        reader.pin()
        completed = reader.completed(now=int(time.time()) + 1, recent=300)
        assert completed.counts.succeeded == 1
    assert not list((tmp_path / "global" / ".runtime" / "instances").glob("*.json"))


@pytest.mark.parametrize("command", ["fork", "rewind"])
def test_offline_edit_chat_starts_a_fresh_chat_executor(tmp_path, monkeypatch, command):
    import io
    import sys
    from toolang.up.instance import read_instance

    source = tmp_path / "aid.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    original = select_roaming(source)
    store = RunStore(original.run_store, read_only=True)
    try:
        (run,) = store.list_runs()
    finally:
        store.close()
    monkeypatch.setattr(sys, "stdin", io.StringIO("/exit\n"))
    assert main([str(source), command, run.id, "--chat"]) == 0
    continued = select_roaming(source)
    assert continued.root != original.root
    entry = read_instance(continued.runtime_status)
    assert entry is not None
    assert entry["control"]["drivers"] == ["chat"]
    assert entry["workload"]["status"] == "stopped"
    assert not list((root / ".runtime" / "instances").glob("*.json"))
