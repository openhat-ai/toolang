"""Module exports keep declaration roles and template ownership in validation."""

import pytest

from toolang.common.layout import AgentLayout
from toolang.state.errors import StatePreparationError
from toolang.state.prepare import prepare_agent_state


def _prepare(tmp_path, root_source, module_source):
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "runnables").mkdir(parents=True)
    layout.program.write_text(root_source)
    (layout.home / "runnables/research.too").write_text(module_source)
    return prepare_agent_state(layout)


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_exported_runnables_reject_whole_handle_arguments(tmp_path, kind, named):
    with pytest.raises(StatePreparationError, match="Run handle 'job'"):
        _prepare(
            tmp_path,
            "agic worker():\n  Work.\nflow parent():\n"
            "  let job = async run worker\n  run research\n",
            f"{kind}{' research' if named else ''}(job: Json):\n  pass\n",
        )


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
    assert state.runnable_modules["research"] == "runnables::research"


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("kind", ["context", "instruct"])
def test_exported_agic_checks_module_history_window(tmp_path, kind, named):
    with pytest.raises(StatePreparationError, match="outside the active window"):
        _prepare(
            tmp_path,
            f"{kind} shared:\n  Root guidance.\n"
            "flow parent():\n  repeat 1 time windowing 1:\n    run research\n",
            f"{kind} shared:\n  {{{{_2}}}}\n"
            f"agic{' research' if named else ''}():\n"
            f"  {kind} = shared\n  Work.\n",
        )


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
        with pytest.raises(StatePreparationError, match="outside the active window"):
            _prepare(tmp_path, root, module)
