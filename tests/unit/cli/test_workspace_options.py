"""Invocation workspace grants, names, and selection are independent of placement."""

from pathlib import Path

import pytest

from toolang.cli.common.workspaces import resolve_workspaces
from toolang.common.layout import AgentLayout
from toolang.base.errors import ToolangError
from toolang.state.prepare import load_agent_state, prepare_agent_state
from toolang.up.mounts import prepare_workspace_mounts


@pytest.fixture(params=["resident", "roaming", "visiting"])
def layout(request, tmp_path):
    result = AgentLayout(tmp_path / ".toolang", "helper", request.param)
    result.home.mkdir(parents=True)
    result.program.write_text("flow run():\n  pass\n")
    return result


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("another_dir", "another-dir"),
        ("=another_dir", "another-dir"),
        ("repo=another_dir", "repo"),
        ("=foo=bar", "foo-bar"),
        ("repo=foo=bar", "repo"),
        ("project.v2", "project-v2"),
    ],
)
def test_paths_names_and_first_equals(layout, tmp_path, argument, expected):
    path = argument.partition("=")[2] if "=" in argument else argument
    (tmp_path / path).mkdir()
    invocation = resolve_workspaces(layout, procdir=tmp_path, workdir=argument)
    assert invocation.workdir == f"{expected}://"
    assert invocation.additions == {expected: str(tmp_path / path)}
    assert not layout.config.exists()


def test_script_default_and_explicit_replacement(layout, tmp_path):
    srcdir = tmp_path / "source"
    srcdir.mkdir()
    procdir = tmp_path / "caller"
    procdir.mkdir()
    assert resolve_workspaces(layout, procdir=procdir).additions == {}
    default = resolve_workspaces(layout, procdir=procdir, srcdir=srcdir)
    assert default.additions == {"source": str(srcdir)}
    assert default.workdir == "source://"
    explicit = resolve_workspaces(layout, procdir=procdir, srcdir=srcdir, paths=["."])
    assert explicit.additions == {"caller": str(procdir)}
    assert explicit.workdir == "caller://"


@pytest.mark.parametrize("explicit_workspaces", [False, True])
@pytest.mark.parametrize("workdir", [None, "project=project", "repo://src"])
def test_script_workspace_fallback_depends_only_on_workspace_option(
    layout, tmp_path, explicit_workspaces, workdir
):
    source = tmp_path / "source"
    source.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    paths = ["one=project", "two=project"] if explicit_workspaces else []

    selected = resolve_workspaces(
        layout, procdir=tmp_path, paths=paths, workdir=workdir, srcdir=source
    )

    expected = (
        {"one": str(project), "two": str(project)}
        if explicit_workspaces
        else {"source": str(source)}
    )
    if workdir == "project=project":
        expected["project"] = str(project)
    assert selected.additions == expected
    assert selected.workdir == (
        "project://"
        if workdir == "project=project"
        else "repo://src"
        if workdir is not None
        else "two://"
        if explicit_workspaces
        else "source://"
    )


def test_uri_selection_and_last_workspace_fallback(layout, tmp_path):
    (tmp_path / "first" / "src").mkdir(parents=True)
    (tmp_path / "last").mkdir()
    paths = ["first", "last"]
    assert (
        resolve_workspaces(layout, procdir=tmp_path, paths=paths).workdir == "last://"
    )
    selected = resolve_workspaces(
        layout, procdir=tmp_path, paths=paths, workdir="first://src"
    )
    assert selected.workdir == "first://src"
    layout.config.write_text('[workspaces]\nrepo = "../../../first"\n')
    existing = resolve_workspaces(layout, procdir=tmp_path, workdir="repo://src")
    assert existing.additions == {}
    assert existing.workdir == "repo://src"


@pytest.mark.parametrize(
    "paths", [["a_b", "a-b"], ["repo=a_b", "repo=a-b"], ["lab=a_b"]]
)
def test_conflicts_fail_without_writing_config(layout, tmp_path, paths):
    (tmp_path / "a_b").mkdir()
    (tmp_path / "a-b").mkdir()
    with pytest.raises(ValueError, match="conflicts"):
        resolve_workspaces(layout, procdir=tmp_path, paths=paths)
    assert not layout.config.exists()


@pytest.mark.parametrize("workdir", ["lab://../escape", "=", "name=", ""])
def test_invalid_selection_rejected(layout, tmp_path, workdir):
    with pytest.raises((ValueError, ToolangError)):
        resolve_workspaces(layout, procdir=tmp_path, workdir=workdir)


def test_temporary_bindings_survive_independent_runs_and_reload(layout, tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    a = prepare_agent_state(layout, workspace_additions={"repo": str(first)})
    b = prepare_agent_state(layout, workspace_additions={"repo": str(second)})
    assert a.revision != b.revision
    assert a.workspaces == {"repo": str(first)}
    assert load_agent_state(layout, a.revision).workspaces == a.workspaces
    assert load_agent_state(layout, b.revision).workspaces == b.workspaces
    assert prepare_agent_state(layout).workspaces == {}
    assert not layout.config.exists()


def test_alias_mounts_share_one_mount(layout, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    mounts, mapping = prepare_workspace_mounts(
        layout.home, Path("/guest"), workspaces={"one": str(root), "two": str(root)}
    )
    assert len([mount for mount in mounts if mount.local_path == root]) == 1
    assert mapping["one"] == mapping["two"]


def test_existing_server_grant_can_be_selected_without_adding(layout, tmp_path):
    root = tmp_path / "remote"
    (root / "src").mkdir(parents=True)
    selected = resolve_workspaces(
        layout,
        procdir=tmp_path,
        workdir="repo://src",
    )
    assert selected.additions == {}
    assert selected.workdir == "repo://src"


def test_script_workdir_can_select_automatic_source_without_duplicate_grant(
    layout, tmp_path
):
    source = tmp_path / "source"
    source.mkdir()
    selected = resolve_workspaces(
        layout, procdir=tmp_path, srcdir=source, workdir="source"
    )
    assert selected.additions == {"source": str(source)}
    assert selected.workdir == "source://"


def test_script_workdir_cannot_rebind_automatic_source_name(layout, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="workspace name 'source' conflicts"):
        resolve_workspaces(
            layout, procdir=tmp_path, srcdir=source, workdir="source=other"
        )


def test_repeated_workdir_is_rejected(layout, tmp_path):
    import typer

    with pytest.raises(typer.BadParameter, match="only be specified once"):
        resolve_workspaces(layout, procdir=tmp_path, workdir=["lab://", "lab://"])


def test_copied_config_rebases_relative_workspaces_in_cached_state(tmp_path):
    import shutil

    original = AgentLayout.resident(tmp_path / "original", "helper")
    original.home.mkdir(parents=True)
    original.program.write_text("flow run():\n  pass\n")
    original.config.write_text('[workspaces]\nrepo = ".."\n')
    previous = prepare_agent_state(original)
    relocated = AgentLayout.resident(tmp_path / "relocated", "helper")
    shutil.copytree(original.root, relocated.root)
    current = prepare_agent_state(relocated)
    assert previous.workspaces == {"repo": str(original.home.parent)}
    assert current.workspaces == {"repo": str(relocated.home.parent)}
    assert current.revision != previous.revision


def test_hosted_startup_transports_captured_grants_without_resolving_again(
    layout, tmp_path
):
    import json
    from toolang.up.server import resolve_serve, build_serve_argv

    bindings = {"repo": str(tmp_path / "host-only")}
    spec = resolve_serve(
        layout=layout, port=7001, workspace_additions=bindings, workdir="repo://src"
    )
    argv = build_serve_argv(spec, root=Path("/guest"))
    assert json.loads(argv[argv.index("--workspace-bindings") + 1]) == bindings
    assert argv[argv.index("--initial-workdir") + 1] == "repo://src"
    if layout.placement != "resident":
        assert argv[argv.index("--placement") + 1] == layout.placement


def test_explicit_grants_can_repeat_running_bindings_when_selecting_uri(
    layout, tmp_path
):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    selected = resolve_workspaces(
        layout,
        procdir=tmp_path,
        paths=["repo=repo"],
        workdir="repo://src",
    )
    assert selected.additions == {"repo": str(root)}
    assert selected.workdir == "repo://src"


def test_configured_lab_does_not_replace_the_implicit_workspace(layout, tmp_path):
    layout.config.write_text('[workspaces]\nlab = "/unavailable-authored-lab"\n')
    (layout.home / "lab" / "src").mkdir(parents=True)
    selected = resolve_workspaces(layout, procdir=tmp_path, workdir="lab://src")
    assert selected.workdir == "lab://src"


def test_remote_workspace_uri_does_not_read_client_config_or_probe_remote_path(
    layout, tmp_path
):
    layout.config.write_text("invalid toml = [")
    selected = resolve_workspaces(
        layout,
        procdir=tmp_path,
        workdir="remote://src",
    )
    assert selected.workdir == "remote://src"
    assert selected.additions == {}


def test_runtime_workspace_uri_is_deferred_without_client_state(layout, tmp_path):
    selected = resolve_workspaces(layout, procdir=tmp_path, workdir="remote://src")
    assert selected.workdir == "remote://src"


def test_running_inspection_uses_server_state_without_local_preparation(
    layout, tmp_path, monkeypatch
):
    from toolang.cli.common.client import RuntimeClient
    from toolang.cli.common.workspaces import running_workspace_inspection
    from toolang.up.process import AgentProcess, AgentStatus
    from toolang.state import prepare

    layout.config.write_text("invalid = [")
    monkeypatch.setattr(
        AgentProcess,
        "status",
        lambda self, **kwargs: AgentStatus(
            name=layout.name,
            status="running",
            endpoint="http://runtime.test",
            api_url=None,
            webui_url=None,
            sandbox="docker",
        ),
    )
    monkeypatch.setattr(
        prepare,
        "prepare_agent_state",
        lambda *args, **kwargs: pytest.fail(
            "remote inspection must not prepare client State"
        ),
    )
    requests = []

    def get(self, path):
        requests.append(path)
        return {
            "revision": "a" * 64,
            "items": [
                {"name": "lab", "path": "/host-only/lab", "available": True},
                {"name": "repo", "path": "/host-only/repo", "available": True},
            ],
            "workdir": "repo://src",
        }

    monkeypatch.setattr(RuntimeClient, "get", get)
    result = running_workspace_inspection(layout, workdir="repo://src")
    assert result is not None
    assert result.workdir == "repo://src"
    assert result.items[-1].available
    assert requests == ["/api/v1/workspaces?workdir=repo%3A%2F%2Fsrc"]


def test_temporary_workspace_cannot_replace_configured_name(layout, tmp_path):
    layout.config.write_text(f'[workspaces]\nrepo = "{tmp_path}"\n')
    selection = resolve_workspaces(layout, procdir=tmp_path, paths=[f"repo={tmp_path}"])
    with pytest.raises(ValueError, match="temporary workspace name already exists"):
        prepare_agent_state(layout, workspace_additions=selection.additions)
