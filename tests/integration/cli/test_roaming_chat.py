"""Roaming Chat owns one foreground runtime and retains its recorded history."""

from contextlib import closing
from io import StringIO
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest
from typer._click.exceptions import ClickException

from toolang.cli.toolang.main import main
from toolang.execution.inspection.history import RunHistory
from toolang.execution.store import RunStore
from toolang.up.allocation import allocate_roaming, retained_roaming, select_roaming
from toolang.up.instance import process_alive, read_instance
from toolang.up.invocation import script_invocation


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "selected root"
    root.mkdir()
    (root / "config.toml").write_text("[teaming]\nenabled = false\n")
    source = tmp_path / "source @space.too"
    source.write_text("flow chat(_: Part[]) -> Part[]:\n  pass\n")
    return root, source


def _command(root, *args, input="/exit\n"):
    return subprocess.run(
        [sys.executable, "-m", "toolang.cli.toolang", "--root", str(root), *args],
        input=input,
        capture_output=True,
        text=True,
        timeout=40,
        env={**os.environ, "TOOLANG_TMUX": "0"},
    )


def _chat(root, selector, *options, input="/exit\n"):
    result = _command(root, str(selector), "chat", *options, input=input)
    assert result.returncode == 0, result.stderr
    return result


def _runs(layout):
    with closing(RunStore(layout.run_store, read_only=True)) as store:
        return store.list_runs()


def _released(layout):
    entry = read_instance(layout.runtime_status)
    assert entry is not None
    assert entry["workload"]["status"] == "stopped"
    assert "script_owner" not in entry["control"]
    assert not list((layout.root / ".runtime" / "active").glob("*/info.json"))
    return entry


@pytest.mark.parametrize("agent_first", [True, False])
def test_chat_allocates_fresh_instances_and_reuses_exact_history(project, agent_first):
    root, source = project
    args = [str(source), "chat"] if agent_first else ["chat", str(source)]
    result = _command(root, *args, input="hello\n/exit\n")
    assert result.returncode == 0, result.stderr
    first = select_roaming(source, toolang_root=root)
    entry = _released(first)
    (run,) = _runs(first)
    assert run.status == "succeeded"
    with closing(RunStore(first.run_store, read_only=True)) as store:
        output = RunHistory(store).get_output(run.id)
        assert output is not None and "hello" in str(output.value)
    _chat(root, f"{source}@{first.place_code}", "-t", run.thread.id, input="again\n")
    assert len(_runs(first)) == 2
    assert _released(first)["launch_id"] != entry["launch_id"]
    _chat(root, source)
    second = select_roaming(source, toolang_root=root)
    assert second.place_code != first.place_code
    assert second.agent_home == first.agent_home
    assert second.lab == first.lab
    assert _runs(second) == []
    _released(second)


def test_record_and_latest_continuations_keep_the_selected_instance(project):
    root, source = project
    _chat(root, source, input="first\n")
    first = select_roaming(source, toolang_root=root)
    (run,) = _runs(first)
    _chat(root, source)
    latest = select_roaming(source, toolang_root=root)
    for record in (run.id, run.thread.id):
        _chat(root, source, "-t", record, input="continued\n")
        assert select_roaming(source, toolang_root=root) == latest
    assert len(_runs(first)) == 3
    _chat(root, f"{source}@{first.place_code}", "-t", input="exact\n")
    _chat(root, f"{source}@", input="second\n")
    _chat(root, f"{source}@", "-t", input="latest\n")
    _chat(root, source, "-t", input="bare latest\n")
    assert len(_runs(first)) == 4
    assert len(_runs(latest)) == 3
    assert len(retained_roaming(source, toolang_root=root)) == 2


@pytest.mark.parametrize("command", ["fork", "rewind"])
def test_history_chat_edits_and_continues_an_older_instance(project, command):
    root, source = project
    _chat(root, source, input="anchor\n")
    first = select_roaming(source, toolang_root=root)
    (run,) = _runs(first)
    _chat(root, source)
    latest = select_roaming(source, toolang_root=root)
    result = _command(root, str(source), command, run.id, "--chat", input="next\n")
    assert result.returncode == 0, result.stderr
    runs = _runs(first)
    assert len(runs) == 2 and all(item.status == "succeeded" for item in runs)
    next_run = next(item for item in runs if item.id != run.id)
    assert (next_run.thread.id != run.thread.id) == (command == "fork")
    assert _runs(latest) == []
    assert select_roaming(source, toolang_root=root) == latest
    _released(first)


@pytest.mark.parametrize("command", ["fork", "rewind"])
def test_chat_startup_failure_preserves_a_committed_history_edit(
    project, command, monkeypatch
):
    from toolang.cli.toolang.commands import thread

    root, source = project
    _chat(root, source, input="anchor\n")
    layout = select_roaming(source, toolang_root=root)
    (run,) = _runs(layout)

    def fail(*_args, **_kwargs):
        raise ClickException("Chat startup failed")

    monkeypatch.setattr(thread, "_open_chat", fail)
    assert main(["--root", str(root), str(source), command, run.id, "--chat"]) == 1
    with closing(RunStore(layout.run_store, read_only=True)) as store:
        threads = store.list_threads()
        assert len(threads) == (2 if command == "fork" else 1)
        if command == "rewind":
            assert not RunHistory(store).thread_view(run.thread.id).runs()
    _released(layout)


@pytest.mark.parametrize(
    "options",
    [
        ["--help"],
        ["--unknown"],
        ["--enable", "none"],
        ["--enable", "invalid"],
        ["--default", "invalid=value"],
        ["-d", ".", "-d", "."],
        ["-w", "missing=/does/not/exist"],
        ["-t"],
        ["-t", "chat_missing"],
    ],
)
def test_invalid_or_help_chat_does_not_allocate_an_instance(project, options, capsys):
    root, source = project
    code = main(["--root", str(root), str(source), "chat", *options])
    if options == ["--help"]:
        assert code == 0
    else:
        assert code != 0
    assert not (source.parent / ".toolang").exists(), capsys.readouterr()


def test_missing_and_ambiguous_continuations_do_not_mutate_history(
    project, monkeypatch
):
    root, source = project
    for selector in (f"{source}@", str(source.with_name("missing.too"))):
        assert main(["--root", str(root), selector, "chat"]) == 1
    homes = [
        allocate_roaming(source, toolang_root=root, register=False) for _ in range(2)
    ]
    for layout in homes:
        with closing(RunStore(layout.run_store)) as store:
            store.create_thread(thread_id="chat_shared")
    before = [layout.run_store.read_bytes() for layout in homes]
    monkeypatch.setattr(sys, "stdin", StringIO("/exit\n"))
    assert main(["--root", str(root), str(source), "chat", "-t", "chat_shared"]) == 1
    assert [layout.run_store.read_bytes() for layout in homes] == before
    assert len(retained_roaming(source, toolang_root=root)) == 2


def test_busy_chat_preserves_the_current_owners_inputs_and_identity(
    project, monkeypatch
):
    from toolang.up import source as sources

    root, source = project
    assert main(["--root", str(root), str(source), "info"]) == 0
    layout = select_roaming(source, toolang_root=root)
    with script_invocation(layout):
        before = layout.runtime_status.read_bytes()
        monkeypatch.setattr(
            sources,
            "materialize_roaming_program",
            lambda *_args, **_kwargs: pytest.fail("busy Chat reprojected inputs"),
        )
        assert main(["--root", str(root), f"{source}@", "chat"]) == 1
        assert layout.runtime_status.read_bytes() == before
        assert not layout.run_store.exists()


def test_failed_chat_initialization_releases_its_owned_server(project, monkeypatch):
    from toolang.cli.toolang.commands.chat import main as chat

    root, source = project

    def fail(*_args, **_kwargs):
        raise chat.RemoteChatError("Chat initialization failed")

    monkeypatch.setattr(chat, "RemoteChatSession", fail)
    assert main(["--root", str(root), str(source), "chat"]) == 1
    layout = select_roaming(source, toolang_root=root)
    _released(layout)
    assert _runs(layout) == []


def test_failed_profile_check_releases_the_new_runtime(project, monkeypatch):
    from toolang.cli.common import agent_server
    from toolang.up.instance import SandboxState
    from toolang.up.sandbox import load_state_sandbox, release_stopped
    import asyncio

    root, source = project

    def fail(_server):
        raise httpx.ConnectError("profile unavailable")

    monkeypatch.setattr(agent_server, "_retain_server", fail)
    try:
        assert main(["--root", str(root), str(source), "chat"]) == 1
        layout = select_roaming(source, toolang_root=root)
        assert SandboxState.load(layout.runtime_status) is None
        _released(layout)
    finally:
        layout = select_roaming(source, toolang_root=root)
        state = SandboxState.load(layout.runtime_status)
        if state is not None:
            implementation = load_state_sandbox(layout, state)
            asyncio.run(implementation.stop(state.ref, force=True))
            asyncio.run(release_stopped(layout))


def test_chat_starts_its_finalization_deadline_before_closing_the_client(
    project, monkeypatch
):
    from toolang.cli.toolang.commands.chat import main as chat
    from toolang.execution.types import SessionSetting

    root, source = project
    closed = []

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def initial_setting(self):
            return SessionSetting(model=None, runnable=None)

        def close(self):
            layout = select_roaming(source, toolang_root=root)
            entry = read_instance(layout.runtime_status)
            assert entry is not None
            assert entry["workload"]["shutdown_reason"] == "Chat ended"
            assert entry["workload"]["shutdown_started_at"] > 0
            closed.append(True)

    monkeypatch.setattr(chat, "RemoteChatSession", Session)
    monkeypatch.setattr(sys, "stdin", StringIO("/exit\n"))
    assert main(["--root", str(root), str(source), "chat"]) == 0
    assert closed == [True]
    _released(select_roaming(source, toolang_root=root))


def test_failed_preparation_releases_ownership_and_can_be_retried(project, monkeypatch):
    from toolang.up import source as sources

    root, source = project

    def fail(*_args, **_kwargs):
        raise OSError("source preparation failed")

    with monkeypatch.context() as patch:
        patch.setattr(sources, "materialize_roaming_program", fail)
        assert main(["--root", str(root), str(source), "chat"]) == 1
    layout = select_roaming(source, toolang_root=root)
    entry = read_instance(layout.runtime_status)
    assert entry is not None and "script_owner" not in entry["control"]
    assert entry["workload"]["status"] == "failed"
    _chat(root, f"{source}@{layout.place_code}", input="retry\n")
    assert len(_runs(layout)) == 1
    _released(layout)


@pytest.mark.parametrize("ending", ["eof", "sigint", "kill"])
def test_foreground_chat_releases_its_server_after_client_exit(project, ending):
    root, source = project
    log_path = source.parent / "chat.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "toolang.cli.toolang",
                "--root",
                str(root),
                str(source),
                "chat",
            ],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            text=True,
        )
        layout = None
        try:
            deadline = time.monotonic() + 35
            while "> " not in log_path.read_text():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.05)
            layout = select_roaming(source, toolang_root=root)
            entry = read_instance(layout.runtime_status)
            assert entry is not None
            workload = entry["workload"]
            assert workload["lifetime"] == "foreground" and workload["drivers"] == [
                "chat"
            ]
            before = layout.runtime_status.read_bytes()
            rejected = _command(root, f"{source}@", "chat")
            assert rejected.returncode == 1 and "HOME is busy" in rejected.stderr
            assert layout.runtime_status.read_bytes() == before
            if ending == "eof":
                _chat(root, source)
                second = select_roaming(source, toolang_root=root)
                assert second.place_code != layout.place_code
                assert second.agent_home == layout.agent_home
                assert layout.runtime_status.read_bytes() == before
            if ending == "kill":
                process.kill()
            elif ending == "sigint":
                process.send_signal(signal.SIGINT)
            else:
                assert process.stdin is not None
                process.stdin.close()
            process.wait(timeout=15)
            deadline = time.monotonic() + 15
            while (
                process_alive(workload["pid"], workload["process_created"]) is not False
            ):
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.05)
            entry = read_instance(layout.runtime_status)
            assert entry is not None and entry["workload"]["status"] == "stopped"
            assert not list((root / ".runtime" / "active").glob("*/info.json"))
            with closing(RunStore(layout.run_store, read_only=True)) as store:
                assert store.list_threads() == []
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdin is not None:
                process.stdin.close()
            if layout is not None:
                import asyncio
                from toolang.up.sandbox import release_stopped

                asyncio.run(release_stopped(layout))
