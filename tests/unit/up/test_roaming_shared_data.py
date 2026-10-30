"""Shared roaming inputs and immutable State with independent runtime ownership."""

import asyncio
import io
import shutil
from concurrent.futures import ThreadPoolExecutor
from tests.support.layout import capture_guest_source
from dataclasses import replace

import pytest

from toolang.cli.toolang.main import main
from toolang.common.layout import AgentLayout
from toolang.common.config_runtime import CONFIG_PATCH_ENV, RuntimeConfig
from toolang.lang.ast import Program
from toolang.state.cache import load_current_agent_revision
from toolang.state.prepare import load_agent_state, prepare_agent_state
from toolang.state.source import ProgramSource
from toolang.state.watcher import StateWatcher
from toolang.up.allocation import allocate_roaming
from toolang.up.source import materialize_roaming_program
from toolang.up.mounts import prepare_guest_environment


def _layout(source, root):
    layout = allocate_roaming(source, toolang_root=root, register=False)
    return materialize_roaming_program(source, layout=layout)


@pytest.mark.parametrize("placement", ["resident", "visiting"])
def test_existing_placements_keep_shared_and_runtime_roots_together(
    tmp_path, placement
):
    layout = (
        AgentLayout.resident(tmp_path, "aid")
        if placement == "resident"
        else AgentLayout.visiting("gh:owner/aid", "aid", root=tmp_path)
    )
    assert layout.agent_root == layout.runtime_root == layout.place
    assert layout.agent_home == layout.home


def test_roaming_shares_authored_state_and_lab_across_instances_and_roots(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    first = _layout(source, tmp_path / "root_one")
    accepted = prepare_agent_state(first)
    (first.lab / "shared.txt").write_text("retained")
    for root in (first.root, tmp_path / "root_two"):
        second = _layout(source, root)
        state = prepare_agent_state(second)
        assert second.agent_root == first.agent_root == tmp_path / ".toolang"
        assert second.agent_home == tmp_path / ".toolang" / "agents" / "aid"
        assert second.runtime_root != first.runtime_root
        assert second.run_store != first.run_store
        assert state.revision == accepted.revision
        assert second.lab == first.lab
        assert (second.lab / "shared.txt").read_text() == "retained"
        assert not (second.runtime_root / ".state").exists()
        assert not (second.home / ".state").exists()
        assert not (second.runtime / "state").exists()


def test_other_scripts_do_not_rewrite_shared_project_inputs(tmp_path):
    root = tmp_path / "root"
    first_source, second_source = tmp_path / "one.too", tmp_path / "two.too"
    first_source.write_text("flow first():\n  pass\n")
    second_source.write_text("flow second():\n  pass\n")
    (tmp_path / "toolang.toml").write_text("# Shared project settings\n")
    first = _layout(first_source, root)
    state = prepare_agent_state(first)
    original_config = first.place_config.read_bytes()
    second = _layout(second_source, root)
    other = prepare_agent_state(second)
    assert first.place_config.read_bytes() == original_config
    assert other.root_revision == state.root_revision
    assert first.program.samefile(first_source)
    assert second.program.samefile(second_source)
    assert set(state.runnables) == {"first"}
    assert set(other.runnables) == {"second"}


def test_concurrent_fresh_instances_derive_one_shared_program(tmp_path, monkeypatch):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    layouts = [_layout(source, tmp_path / "root") for _ in range(4)]
    original = ProgramSource.parse
    parsed = []

    def parse(self, **kwargs):
        parsed.append(self.digest)
        return original(self, **kwargs)

    monkeypatch.setattr(ProgramSource, "parse", parse)
    with ThreadPoolExecutor(max_workers=4) as pool:
        states = list(pool.map(prepare_agent_state, layouts))
    assert len(parsed) == 1
    assert len({state.revision for state in states}) == 1
    assert len({layout.runtime_status for layout in layouts}) == 4


def test_invocation_bindings_never_replace_shared_current(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    first = _layout(source, tmp_path / "root")
    second = _layout(source, first.root)

    async def exercise():
        one = StateWatcher(first, workspace_additions={"one": str(tmp_path)})
        two = StateWatcher(
            second,
            workspace_additions={"two": str(tmp_path)},
            allow_overrides={"skills": ()},
        )
        state_one = await one.refresh()
        state_two = await two.refresh()
        authored = load_agent_state(first)
        assert not authored.workspace_additions
        assert not authored.allow_overrides
        assert state_one.workspace_additions == {"one": str(tmp_path)}
        assert state_two.workspace_additions == {"two": str(tmp_path)}
        assert (
            load_agent_state(first, state_one.revision).revision == state_one.revision
        )
        assert (await one.refresh()).revision == state_one.revision
        assert (await two.refresh()).revision == state_two.revision
        assert load_current_agent_revision(first) == authored.revision
        source.write_text("flow updated():\n  pass\n")
        updated = await two.refresh()
        assert set(updated.runnables) == {"updated"}
        assert set(one.current().runnables) == {"main"}
        assert one.current().workspace_additions == {"one": str(tmp_path)}

    asyncio.run(exercise())


def test_fresh_instances_reuse_the_same_effective_composition(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    first = _layout(source, tmp_path / "root_one")
    second = _layout(source, tmp_path / "root_two")
    additions = {"repo": str(tmp_path)}
    accepted = prepare_agent_state(first, workspace_additions=additions)
    authored = load_current_agent_revision(first)
    revisions = set((first.agent_state / "v24" / "revs").iterdir())
    repeated = prepare_agent_state(second, workspace_additions=additions)
    assert repeated.revision == accepted.revision
    assert set((first.agent_state / "v24" / "revs").iterdir()) == revisions
    assert load_current_agent_revision(first) == authored
    assert authored != accepted.revision


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_warm_cli_uses_prepared_program_in_a_fresh_process_cache(
    tmp_path, monkeypatch, capsys, newline
):
    from toolang.state import source as sources

    source = tmp_path / "aid.too"
    source.write_bytes(f"flow main(_: Text) -> Text:{newline}  pass{newline}".encode())
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    original = Program.from_source
    parsed = []

    def parse(text, **kwargs):
        parsed.append(text)
        return original(text, **kwargs)

    monkeypatch.setattr(Program, "from_source", staticmethod(parse))
    assert main([str(source), "main", "--quiet", "hello"]) == 0
    assert parsed
    parsed.clear()
    sources._AGENT_PROGRAMS.clear()
    capsys.readouterr()
    assert main([str(source), "main", "hello"]) == 0
    assert not parsed
    assert "Prepared state" not in capsys.readouterr().err


def test_guest_reuses_shared_layers(tmp_path, monkeypatch):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    host = _layout(source, tmp_path / "root")
    accepted = prepare_agent_state(host)
    guest_root = tmp_path / "guest"
    guest_root.mkdir()
    guest = replace(
        host,
        place=guest_root,
        root=guest_root,
        agent_root_path=guest_root,
        runtime_config=RuntimeConfig.parse(
            prepare_guest_environment(host, mapping={})[CONFIG_PATCH_ENV]
        ),
    )
    for path in (host.root_config, host.root_source_receipt):
        if path.exists():
            shutil.copy2(path, guest.agent_root / path.name)
    shutil.copytree(host.place_state.parent, guest.place_state.parent)
    shutil.copytree(host.agent_home, guest.agent_home)

    capture_guest_source(host, guest, state_revision=accepted.revision)

    def unexpected_parse(*args, **kwargs):
        pytest.fail("guest reparsed already prepared source")

    monkeypatch.setattr(ProgramSource, "parse", unexpected_parse)
    loaded = prepare_agent_state(guest)
    assert loaded.revision == accepted.revision


def test_roaming_shared_home_rejects_a_directory_symlink(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow main():\n  pass\n")
    layout = allocate_roaming(source, toolang_root=tmp_path / "root", register=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.agent_home.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        materialize_roaming_program(source, layout=layout)
    assert not list(outside.iterdir())
