import time

from toolang.cli.toolang.main import main
from toolang.execution.store import RunStore
from toolang.stats.recent import recent_source
from toolang.up.identity import select_roaming


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
