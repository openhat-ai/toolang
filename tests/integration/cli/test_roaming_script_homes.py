"""Roaming scripts select durable HOMEs and own them exclusively."""

from contextlib import closing

import pytest

from toolang.cli.toolang.main import main
from toolang.execution.store import RunStore
from toolang.up.allocation import retained_roaming, select_roaming
from toolang.up.instance import read_instance
from toolang.up.invocation import script_invocation


@pytest.fixture
def source_home(tmp_path, monkeypatch):
    root = tmp_path / "selected root"
    source = tmp_path / "source @space.too"
    source.write_text("flow main(_: Text) -> Text:\n  pass\n")
    monkeypatch.setenv("TOOLANG_ROOT", str(root))
    return source, root


def test_info_prepares_one_root_scoped_home_and_explicit_run_reuses_it(source_home):
    source, root = source_home
    assert main([str(source), "info"]) == 0
    home = select_roaming(source, toolang_root=root)
    original = read_instance(home.runtime_status)
    assert original is not None and original["workload"]["status"] == "stopped"
    assert not home.run_store.exists()
    assert not list((root / ".runtime" / "active").glob("*/info.json"))
    assert home.agent_home == source.parent / ".toolang" / "agents" / home.name
    assert home.agent_home.is_dir()
    assert home.agent_home != home.home
    assert main(["info", f"{source}@"]) == 0
    assert retained_roaming(source, toolang_root=root) == (home,)
    for operand in (f"{source}@", f"{source}@{home.place_code}"):
        assert main(["run", operand, "main", "--quiet", "hello"]) == 0
    with closing(RunStore(home.run_store, read_only=True)) as store:
        assert len(store.list_runs()) == 2
        assert all(run.status == "succeeded" for run in store.list_runs())
    assert main([str(source), "main", "--quiet", "new"]) == 0
    latest = select_roaming(source, toolang_root=root)
    assert latest != home
    assert main([f"{source}@{home.place_code}", "main", "--quiet", "old"]) == 0
    assert select_roaming(source, toolang_root=root) == latest


def test_fresh_cli_processes_reuse_shared_state_across_roots(source_home):
    import os
    import subprocess
    import sys

    source, root = source_home
    shared = source.parent / ".toolang"
    revisions = None
    for selected in (root, root, root.with_name("another root")):
        result = subprocess.run(
            [sys.executable, "-m", "toolang.cli.toolang", str(source), "main", "hello"],
            env={**os.environ, "TOOLANG_ROOT": str(selected)},
            input="",
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        current = set(shared.glob("**/revs/*"))
        if revisions is None:
            revisions = current
            assert revisions
        else:
            assert current == revisions
            assert "Prepared state" not in result.stderr
    layouts = (
        *retained_roaming(source, toolang_root=root),
        *retained_roaming(source, toolang_root=root.with_name("another root")),
    )
    assert len(layouts) == len({layout.run_store for layout in layouts}) == 3
    assert len({layout.agent_home for layout in layouts}) == 1


def test_explicit_missing_home_does_not_prepare_another(source_home):
    source, root = source_home
    assert main([f"{source}@", "main", "--quiet", "hello"]) != 0
    assert main(["info", f"{source}@"]) != 0
    assert not retained_roaming(source, toolang_root=root)


def test_busy_script_and_rerun_cannot_change_the_current_owner(source_home, capsys):
    source, root = source_home
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    home = select_roaming(source, toolang_root=root)
    with closing(RunStore(home.run_store, read_only=True)) as store:
        run = store.list_runs()[0]
    with script_invocation(home):
        before = read_instance(home.runtime_status)
        assert main([f"{source}@", "main", "--quiet", "second"]) == 1
        assert main([f"{source}@", "rerun", run.id]) == 1
        assert "HOME is busy" in capsys.readouterr().err
        assert read_instance(home.runtime_status) == before
    assert main([f"{source}@", "main", "--quiet", "released"]) == 0


def test_info_does_not_fetch_inputs_or_renew_an_owner(source_home, monkeypatch):
    from toolang.up import source as sources

    source, root = source_home
    assert main(["info", str(source)]) == 0
    home = select_roaming(source, toolang_root=root)
    with script_invocation(home):
        before = read_instance(home.runtime_status)
        monkeypatch.setattr(
            sources,
            "materialize_roaming_program",
            lambda *_args, **_kwargs: pytest.fail("info fetched active inputs"),
        )
        assert main(["info", f"{source}@"]) == 0
        assert read_instance(home.runtime_status) == before


def test_roaming_hosting_fails_before_mutation(source_home):
    source, root = source_home
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    home = select_roaming(source, toolang_root=root)
    before = home.run_store.read_bytes()
    for command in ("start", "serve", "stop"):
        assert main([f"{source}@", command]) != 0
    assert home.run_store.read_bytes() == before
    assert retained_roaming(source, toolang_root=root) == (home,)


def test_delayed_closing_intent_cannot_touch_a_replacement_owner(source_home):
    source, root = source_home
    assert main(["info", str(source)]) == 0
    home = select_roaming(source, toolang_root=root)
    with script_invocation(home) as old:
        assert old is not None
    old.enabled = False
    try:
        with script_invocation(home):
            before = read_instance(home.runtime_status)
            old.begin("late old cleanup")
            assert read_instance(home.runtime_status) == before
    finally:
        old.close()


def test_concurrent_first_info_allocates_only_one_home(source_home):
    import os
    import subprocess
    import sys

    source, root = source_home
    commands = [
        [sys.executable, "-m", "toolang.cli.toolang", "info", str(source)]
        for _ in range(2)
    ]
    workers = [
        subprocess.Popen(
            command,
            env={**os.environ, "TOOLANG_ROOT": str(root)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for command in commands
    ]
    try:
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, (stdout, stderr)
        (home,) = retained_roaming(source, toolang_root=root)
        assert home.program.samefile(source)
        assert not home.run_store.exists()
        assert not list((root / ".runtime" / "active").glob("*/info.json"))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)


def test_failed_info_preparation_retries_in_the_same_home(source_home):
    source, root = source_home
    source.write_text("this is not a valid Toolang program\n")
    assert main(["info", str(source)]) == 1
    (home,) = retained_roaming(source, toolang_root=root)
    assert not home.run_store.exists()
    source.write_text("flow main():\n  pass\n")
    assert main(["info", str(source)]) == 0
    assert retained_roaming(source, toolang_root=root) == (home,)
    assert not list((root / ".runtime" / "active").glob("*/info.json"))


def test_rerun_claims_a_stopped_home_and_keeps_its_history(source_home):
    source, root = source_home
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    home = select_roaming(source, toolang_root=root)
    original = read_instance(home.runtime_status)
    assert original is not None
    with closing(RunStore(home.run_store, read_only=True)) as store:
        run = store.list_runs()[0]
    assert main([f"{source}@", "rerun", run.id, "--sandbox", "host"]) == 0
    latest = read_instance(home.runtime_status)
    assert latest is not None and latest["launch_id"] != original["launch_id"]
    assert "script_owner" not in latest["control"]
    assert retained_roaming(source, toolang_root=root) == (home,)
    with closing(RunStore(home.run_store, read_only=True)) as store:
        assert len(store.list_runs()) == 2
        assert all(run.status == "succeeded" for run in store.list_runs())
