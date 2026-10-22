import time
from contextlib import closing

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
    assert not list((tmp_path / ".toolang").glob("*/s*"))


def test_roaming_argument_failure_does_not_publish_an_identity(tmp_path, monkeypatch):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    assert main([str(source), "chat", "--unknown"]) == 2
    assert not (tmp_path / ".toolang").exists()
    assert not list((root / ".runtime" / "active").glob("*.json"))


def test_duplicate_chat_workdir_does_not_prepare_a_roaming_instance(
    tmp_path, monkeypatch, capsys
):
    from toolang.up import source as sources

    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "global"))
    monkeypatch.setattr(
        sources,
        "materialize_roaming_program",
        lambda *_args, **_kwargs: pytest.fail("invalid workdir prepared a source"),
    )
    before = set(tmp_path.rglob("*"))
    assert main([str(source), "chat", "-d", ".", "--workdir", "."]) == 2
    assert "--workdir may only be specified once" in capsys.readouterr().err
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("option", ["-i", "--instance"])
@pytest.mark.parametrize("command", ["info", "inspect"])
@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
def test_removed_instance_options_fail_without_acquisition(
    tmp_path, monkeypatch, capsys, command, placement, option
):
    from toolang.catalog.agent import LocalAgents
    from toolang.up import source as sources

    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    if placement == "resident":
        LocalAgents(root / "agents").create("aid", content="flow main():\n  pass\n")
        selector = "aid"
    elif placement == "visiting":
        selector = "gh:owner/aid"
    else:
        source = tmp_path / "aid.too"
        source.write_text("flow main():\n  pass\n")
        selector = str(source)
    monkeypatch.setattr(
        sources,
        "resolve_visiting_layout",
        lambda *_args, **_kwargs: pytest.fail("instance validation fetched a source"),
    )
    args = [selector, "info"] if command == "info" else [selector, "inspect", "runs"]
    before = set(tmp_path.rglob("*"))
    assert main([*args, option, "unused"]) == 2
    error = capsys.readouterr().err
    assert f"No such option: {option}" in error
    assert set(tmp_path.rglob("*")) == before


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
    entry = read_instance(select_roaming(source, toolang_root=root).runtime_status)
    assert entry is not None and entry["workload"]["status"] == "failed"
    assert not list((root / ".runtime" / "active").glob("*.json"))


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
@pytest.mark.parametrize("explicit_instance", [False, True])
def test_roaming_rejects_hosting_commands_without_mutating_instances(
    tmp_path, monkeypatch, command, explicit_instance
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
        for path in (root / ".runtime" / "active").glob("*.json")
    }
    args = [
        f"{source}@{layout.instance_id}" if explicit_instance else str(source),
        command,
    ]
    assert main(args) == 2
    assert layout.runtime_status.read_bytes() == before
    assert list((source.parent / ".toolang").glob("*/s*")) == [layout.root]
    assert {
        path.name: path.read_bytes()
        for path in (root / ".runtime" / "active").glob("*.json")
    } == references


def test_retained_selection_and_rerun_preserve_the_selected_home(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "aid.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "global"))
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    first = select_roaming(source, toolang_root=tmp_path / "global")
    assert first.instance_id is not None
    with_store = RunStore(first.run_store, read_only=True)
    run = with_store.list_runs()[0]
    with_store.close()
    assert main([str(source), "main", "--quiet", "again"]) == 0
    second = select_roaming(source, toolang_root=tmp_path / "global")
    assert first.root != second.root
    assert main([f"{source}@{first.instance_id}", "inspect", "runs"]) == 0
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
        assert main([f"{source}@{first.instance_id}", "fork", run.id]) == 0
    assert main([f"{source}@{first.instance_id}", "rerun", run.id]) == 0
    assert select_roaming(source, toolang_root=tmp_path / "global").root == second.root
    with closing(RunStore(first.run_store, read_only=True)) as store:
        runs = store.list_runs()
        assert len(runs) == 2
        assert all(not store.is_imported(item.id) for item in runs)
        assert all(item.status == "succeeded" for item in runs)
    with recent_source(first.run_store) as reader:
        reader.pin()
        completed = reader.completed(now=int(time.time()) + 1, recent=300)
        assert completed.counts.succeeded == 2
    assert not list((tmp_path / "global" / ".runtime" / "active").glob("*.json"))


@pytest.mark.parametrize("command", ["fork", "rewind"])
def test_offline_edit_chat_restarts_in_the_same_home(tmp_path, monkeypatch, command):
    import io
    import sys
    from toolang.up.instance import read_instance

    source = tmp_path / "aid.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    root = tmp_path / "global"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    original = select_roaming(source, toolang_root=root)
    store = RunStore(original.run_store, read_only=True)
    try:
        (run,) = store.list_runs()
    finally:
        store.close()
    monkeypatch.setattr(sys, "stdin", io.StringIO("/exit\n"))
    assert main([str(source), command, run.id, "--chat"]) == 0
    continued = select_roaming(source, toolang_root=root)
    assert continued.root == original.root
    entry = read_instance(continued.runtime_status)
    assert entry is not None
    assert entry["control"]["drivers"] == ["chat"]
    assert entry["workload"]["status"] == "stopped"
    assert not list((root / ".runtime" / "active").glob("*.json"))


@pytest.mark.parametrize("command", ["rerun", "fork", "rewind"])
def test_roaming_continuation_rejects_a_different_toolang_root(
    tmp_path, monkeypatch, command
):
    source = tmp_path / "aid.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    root = tmp_path / "environment-root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    original = select_roaming(source, toolang_root=root)
    with closing(RunStore(original.run_store, read_only=True)) as store:
        (run,) = store.list_runs()
    before = original.runtime_status.read_bytes()
    selected_root = tmp_path / "invocation-root"
    assert (
        main(
            [
                "--root",
                str(selected_root),
                str(source),
                command,
                run.id,
                *(["--chat"] if command != "rerun" else []),
            ]
        )
        == 1
    )
    assert original.runtime_status.read_bytes() == before
    assert not selected_root.exists()


def test_record_selection_binds_an_older_home_without_changing_the_default(
    tmp_path, monkeypatch
):
    source = tmp_path / "aid.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    root = tmp_path / "root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    assert main([str(source), "main", "--quiet", "first"]) == 0
    first = select_roaming(source, toolang_root=root)
    with closing(RunStore(first.run_store, read_only=True)) as store:
        (run,) = store.list_runs()
    assert main([f"{source}@{first.instance_id}", "rerun", run.id]) == 0
    with closing(RunStore(first.run_store, read_only=True)) as store:
        run = next(item for item in store.list_runs() if item.id != run.id)
    assert main([str(source), "main", "--quiet", "second"]) == 0
    latest = select_roaming(source, toolang_root=root)
    assert main([str(source), "rerun", run.id]) == 0
    with closing(RunStore(first.run_store, read_only=True)) as store:
        assert len(store.list_runs()) == 3
    assert select_roaming(source, toolang_root=root).root == latest.root


def test_chat_thread_selection_requires_one_matching_home(
    tmp_path, monkeypatch, capsys
):
    from toolang.cli.common.context import context_layout
    from toolang.cli.toolang.commands.chat import main as chat
    from toolang.up.identity import allocate_roaming

    root = tmp_path / "root"
    source = tmp_path / "aid.too"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    first = allocate_roaming(source, toolang_root=root)
    second = allocate_roaming(source, toolang_root=root)
    for layout, thread in ((first, "chat_first"), (second, "chat_second")):
        with closing(RunStore(layout.run_store)) as store:
            store.create_thread(thread_id=thread)
            store.create_thread(thread_id="chat_shared")
    selected = []
    monkeypatch.setattr(
        chat,
        "chat_command",
        lambda ctx, **_kwargs: selected.append(context_layout(ctx).root),
    )
    assert main([str(source), "chat", "-t", "chat_first"]) == 0
    assert selected == [first.root]
    assert main([str(source), "chat", "-t", "chat_shared"]) == 1
    assert "specify name@ID or file.too@ID" in capsys.readouterr().err
    assert selected == [first.root]
    assert main([f"{source}@{first.instance_id}", "chat", "-t", "chat_shared"]) == 0
    assert selected == [first.root, first.root]
    assert select_roaming(source, toolang_root=root).root == second.root


def test_info_copy_command_binds_the_offline_home_and_quotes_paths(
    tmp_path, monkeypatch
):
    import shlex
    from toolang.cli.toolang.commands import agent
    from toolang.up.identity import allocate_roaming
    from toolang.up.workload import fail_preparation

    source = tmp_path / "a id.too"
    root = tmp_path / "toolang root"
    layout = allocate_roaming(source, toolang_root=root)
    fail_preparation(layout, RuntimeError("finished"))
    captured = {}
    monkeypatch.setattr(
        agent, "echo_pairs_table", lambda rows, **_kwargs: captured.update(rows)
    )
    selector = f"{source}@{layout.instance_id}"
    assert main(["--root", str(root), selector, "info"]) == 0
    assert captured["Toolang root"] == str(root)
    assert captured["Agent root"] == str(layout.root)
    assert captured["Agent"] == layout.instance_name
    assert shlex.split(captured["Command"]) == [
        "too",
        "--root",
        str(root),
        selector,
        "info",
    ]


def test_visiting_instance_chat_uses_cached_home_without_fetching(
    tmp_path, monkeypatch
):
    import asyncio
    from io import StringIO
    from toolang.up.source import HttpRef, materialize_visiting_program
    from toolang.up import source as sources
    from toolang.up.instance import read_instance
    from toolang.up.process import AgentProcess

    root = tmp_path / "root"
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    layout = materialize_visiting_program(
        HttpRef("https://example.test/aid.too"),
        "flow main():\n  pass\n",
        toolang_root=root,
    )
    before = read_instance(layout.runtime_status)
    assert before is not None
    monkeypatch.setattr(
        sources,
        "_fetch_http_text",
        lambda *_args, **_kwargs: pytest.fail("bound Chat fetched its source"),
    )
    monkeypatch.setattr("sys.stdin", StringIO("/exit\n"))
    try:
        assert main([layout.instance_name, "chat"]) == 0
        after = read_instance(layout.runtime_status)
        assert after is not None and after["launch_id"] != before["launch_id"]
        assert after["control"]["root"] == str(layout.root)
    finally:
        entry = read_instance(layout.runtime_status)
        if entry is not None and entry["launch_id"] != before["launch_id"]:
            asyncio.run(
                AgentProcess(layout).stop(
                    force=True, expected_launch_id=entry["launch_id"]
                )
            )
