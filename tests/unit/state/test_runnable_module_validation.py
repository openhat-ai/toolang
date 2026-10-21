"""Module exports keep declaration roles and template ownership in validation."""

import re
import pytest

from toolang.common.layout import AgentLayout
from toolang.state.watcher import StateWatcher
from toolang.state.prepare import prepare_agent_state


def _assert_excluded(state, match=None):
    excluded = [file for file in state.files if not file.accepted]
    assert excluded, "invalid source must be excluded from the published State"
    assert all(file.code and file.message for file in excluded)
    if match is not None:
        assert re.search(match, "; ".join(file.message or "" for file in excluded))
    return excluded


def _prepare(tmp_path, root_source, module_source):
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "runnables").mkdir(parents=True)
    layout.program.write_text(root_source)
    (layout.home / "runnables/research.too").write_text(module_source)
    return prepare_agent_state(layout)


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("root_entry", [False, True])
@pytest.mark.parametrize("module_name", ["_", "agent"])
def test_module_filename_cannot_use_reserved_module_names(
    tmp_path, kind, root_entry, module_name
):
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "runnables").mkdir(parents=True)
    layout.program.write_text("agic:\n  Root.\n" if root_entry else "")
    (layout.home / f"runnables/{module_name}.too").write_text(f"{kind}:\n  pass\n")

    state = prepare_agent_state(layout)
    _assert_excluded(state, match="filename is reserved")
    assert state.toolang_root is not None
    error = StateWatcher(
        AgentLayout.resident(state.toolang_root, state.name), initial_state=state
    ).diagnostics()

    assert error[0].code == "invalid-runnable-filename"
    assert error[0].authored_path == f"runnables/{module_name}.too"


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_exported_runnables_reject_whole_handle_arguments(tmp_path, kind, named):
    state = _prepare(
        tmp_path,
        "agic worker():\n  Work.\nflow parent():\n"
        "  let job = async run worker\n  run research\n",
        f"{kind}{' research' if named else ''}(job: Json):\n  pass\n",
    )
    _assert_excluded(state, match="Run handle 'job'")


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_remote_seek_ignores_same_named_module_export(tmp_path, kind, named):
    state = _prepare(
        tmp_path,
        "flow parent():\n  seek reviewer research\n",
        f"{kind}{' research' if named else ''}(local_only: Text):\n  pass\n",
    )
    assert set(state.runnables) == {"parent", "research"}


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("kind", ["context", "instruct"])
@pytest.mark.parametrize("selection", ["shared", "default"])
def test_exported_agic_uses_its_own_history_templates(tmp_path, kind, named, selection):
    declaration = f"{kind}{' shared' if selection == 'shared' else ''}"
    root = (
        f"{declaration}:\n  {{{{_2}}}}\n"
        "flow parent():\n  repeat 1 time windowing 1:\n    run research\n"
    )
    module = (
        f"{declaration}:\n  Module guidance.\n"
        f"agic{' research' if named else ''}():\n"
        f"  {kind} = {selection}\n  Work.\n"
    )
    state = _prepare(tmp_path, root, module)
    assert state.runnable_modules["research"] == "research"


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("kind", ["context", "instruct"])
def test_exported_agic_checks_module_history_window(tmp_path, kind, named):
    state = _prepare(
        tmp_path,
        f"{kind} shared:\n  Root guidance.\n"
        "flow parent():\n  repeat 1 time windowing 1:\n    run research\n",
        f"{kind} shared:\n  {{{{_2}}}}\n"
        f"agic{' research' if named else ''}():\n"
        f"  {kind} = shared\n  Work.\n",
    )
    _assert_excluded(state, match="outside the active window")


@pytest.mark.parametrize("kind", ["context", "instruct"])
@pytest.mark.parametrize("disabled", [False, True])
def test_exported_agic_retains_inherited_template_owner(tmp_path, kind, disabled):
    root = (
        f"{kind} shared:\n  {{{{_2}}}}\nflow parent():\n"
        f"  {kind} = shared\n"
        "  repeat 1 time windowing 1:\n    run research\n"
    )
    module = (
        f"{kind} shared:\n  Module guidance.\nagic():\n"
        + (f"  {kind} = none\n" if disabled else "")
        + "  Work.\n"
    )
    if disabled:
        _prepare(tmp_path, root, module)
    else:
        state = _prepare(tmp_path, root, module)
        _assert_excluded(state, match="outside the active window")
