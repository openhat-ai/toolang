from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.execution.tools.me import create_toolset
from toolang.execution.tools.me.types import MeToolContext


@pytest.fixture
def context(tmp_path: Path) -> MeToolContext:
    layout = AgentLayout.resident(tmp_path / "toolang", "parrot")
    layout.home.mkdir(parents=True)
    layout.program.write_text("# File header\n\nagic parrot(_: Text):\n  {{_}}\n")
    return MeToolContext(home=layout.home, room=layout.tool_room("me"), layout=layout)


def invoke(context: MeToolContext, operation: str, **arguments: object):
    if operation in {"create", "update", "delete"} and "if_digest" not in arguments:
        arguments["if_digest"] = sha256(context.layout.program.read_bytes()).hexdigest()
    return asyncio.run(
        create_toolset({})
        .tools()[operation]
        .invoke({"kind": "program", **arguments}, context)
    )


def test_program_crud_and_whole_replacement(context: MeToolContext) -> None:
    original = context.layout.program.read_text()
    read = invoke(context, "get")
    assert read.error is None
    whole = read.output["item"]
    assert whole["key"] is None
    assert whole["content"]["source"] == original
    digest = whole["digest"]
    listed = invoke(context, "list").output["items"]
    assert [item["key"] for item in listed] == ["agic:parrot"]
    assert listed[0]["digest"] == digest
    assert "content" not in listed[0]
    source = "## Evaluate repetition.\nagic evaluate():\n  Return true.\n"
    created = invoke(context, "create", key="agic:evaluate", content={"source": source})
    assert created.error is None
    assert created.output["created"] is True
    item = created.output["item"]
    assert item["content"]["source"] == source
    assert item["digest"] == sha256(context.layout.program.read_bytes()).hexdigest()
    assert context.layout.program.read_text() == original + "\n" + source
    replacement = "# New evaluator\nagic evaluate():\n  Return false.\n"
    updated = invoke(
        context,
        "update",
        key="agic:evaluate",
        content={"source": replacement},
        if_digest=item["digest"],
    )
    assert updated.error is None
    assert updated.output["changed"] is True
    assert "## Evaluate repetition." not in context.layout.program.read_text()
    identical = invoke(
        context, "update", key="agic:evaluate", content={"source": replacement}
    )
    assert identical.output["changed"] is False
    deleted = invoke(
        context,
        "delete",
        key="agic:evaluate",
        if_digest=updated.output["item"]["digest"],
    )
    assert deleted.error is None
    assert deleted.output["deleted"] is True
    assert context.layout.program.read_text() == original + "\n"
    replaced = invoke(context, "update", content={"source": "agic:\n  New agent.\n"})
    assert replaced.error is None
    assert context.layout.program.read_text() == "agic:\n  New agent.\n"
    assert invoke(context, "get", key="agic:_").error is None
    assert not context.layout.agent_state.exists()
    assert not context.layout.home_state.exists()


@pytest.mark.parametrize(
    ("operation", "arguments", "code"),
    [
        ("create", {"content": {"source": "agic:\n  Hi.\n"}}, "invalid_request"),
        ("delete", {}, "invalid_request"),
        ("get", {"key": ""}, "invalid_request"),
        ("get", {"key": None}, "invalid_request"),
        ("get", {"key": "parrot"}, "invalid_request"),
        ("get", {"key": "other:parrot"}, "invalid_request"),
        ("get", {"key": "agic:../parrot"}, "invalid_request"),
        ("get", {"key": "psyche:_"}, "invalid_request"),
        ("get", {"key": "agic:missing"}, "not_found"),
        ("delete", {"key": "agic:missing"}, "not_found"),
        (
            "update",
            {"key": "agic:missing", "content": {"source": "agic missing:\n  Hi.\n"}},
            "not_found",
        ),
        (
            "create",
            {"key": "agic:parrot", "content": {"source": "agic parrot:\n  Hi.\n"}},
            "conflict",
        ),
        (
            "update",
            {"key": "agic:parrot", "content": {"source": "agic other:\n  Hi.\n"}},
            "invalid_program",
        ),
        (
            "create",
            {
                "key": "agic:other",
                "content": {"source": "agic other:\n  Hi.\nagic extra:\n  Hi.\n"},
            },
            "invalid_program",
        ),
        (
            "update",
            {"content": {"source": "flow:\n  run missing\n"}},
            "invalid_program",
        ),
        ("update", {"content": {"source": "agic ("}}, "invalid_program"),
        ("update", {"content": {"body": "Hi."}}, "invalid_content"),
        (
            "update",
            {"content": {"source": "agic:\n  Hi.\n"}, "if_digest": "0" * 64},
            "digest_mismatch",
        ),
    ],
)
def test_program_errors_leave_source_unchanged(
    context, operation, arguments, code
) -> None:
    before = context.layout.program.read_bytes()
    result = invoke(context, operation, **arguments)
    assert result.error is not None, result.output
    assert result.output["error"]["code"] == code, result.output
    assert context.layout.program.read_bytes() == before


def test_delete_checks_references_and_composition(context: MeToolContext) -> None:
    source = "agic helper():\n  Hi.\nflow main():\n  run helper\n"
    context.layout.program.write_text(source)
    result = invoke(context, "delete", key="agic:helper")
    assert result.output["error"]["code"] == "invalid_program"
    assert result.output["error"]["issues"][0]["line"] is not None
    assert context.layout.program.read_text() == source
    flows = context.home / "flows"
    flows.mkdir()
    (flows / "research.too").write_text("flow:\n  pass\n")
    conflict = invoke(
        context,
        "create",
        key="agic:research",
        content={"source": "agic research:\n  Hi.\n"},
    )
    assert conflict.output["error"]["code"] == "invalid_program"
    # Main source may refer to an authored flow when validated as a composition.
    valid = invoke(context, "update", content={"source": "flow:\n  run research\n"})
    assert valid.error is None, valid.output


@pytest.mark.parametrize("broken", [b"agic (", b"\xff"])
def test_whole_replacement_repairs_invalid_source(context, broken) -> None:
    context.layout.program.write_bytes(broken)
    if broken != b"\xff":
        assert (
            invoke(context, "get").output["item"]["content"]["source"]
            == broken.decode()
        )
        assert invoke(context, "list").output["error"]["code"] == "invalid_program"
    result = invoke(
        context,
        "update",
        content={"source": "agic:\n  Fixed.\n"},
        if_digest=sha256(broken).hexdigest(),
    )
    assert result.error is None
    assert context.layout.program.read_text() == "agic:\n  Fixed.\n"


def test_two_writers_cannot_overwrite_the_same_snapshot(context: MeToolContext) -> None:
    digest = invoke(context, "get").output["item"]["digest"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                invoke,
                context,
                "update",
                key="agic:parrot",
                content={"source": f"agic parrot:\n  Version {i}.\n"},
                if_digest=digest,
            )
            for i in range(2)
        ]
        results = [future.result() for future in futures]
    assert sum(result.error is None for result in results) == 1
    assert (
        next(result for result in results if result.error).output["error"]["code"]
        == "digest_mismatch"
    )


def test_program_rejects_unowned_symlink_and_linked_flow(context, tmp_path) -> None:
    external = tmp_path / "external.too"
    external.write_text("agic:\n  External.\n")
    context.layout.program.unlink()
    context.layout.program.symlink_to(external)
    result = invoke(context, "update", content={"source": "agic:\n  Changed.\n"})
    assert result.output["error"]["code"] == "storage_error"
    assert external.read_text() == "agic:\n  External.\n"
    context.layout.program.unlink()
    context.layout.program.write_text("agic:\n  Hi.\n")
    flows = context.home / "flows"
    flows.mkdir()
    (flows / "linked.too").symlink_to(external)
    result = invoke(context, "update", content={"source": "agic:\n  Changed.\n"})
    assert result.output["error"]["code"] == "storage_error"


def test_roaming_program_edits_original_and_preserves_runtime_link(tmp_path) -> None:
    from toolang.up.process import materialize_roaming_program

    source = tmp_path / "parrot.too"
    source.write_text("agic:\n  Original.\n")
    source.chmod(0o755)
    layout = materialize_roaming_program(source)
    context = MeToolContext(
        home=layout.home, room=layout.tool_room("me"), layout=layout
    )
    result = invoke(context, "update", content={"source": "agic:\n  Changed.\n"})
    assert result.error is None, result.output
    assert source.read_text() == "agic:\n  Changed.\n"
    assert source.stat().st_mode & 0o777 == 0o755
    assert layout.program.is_symlink()
    assert layout.program.resolve() == source
    from toolang.catalog.config import ConfiguredCaps
    from toolang.state.config import ConfiguredWorkspaces

    config = layout.home / "config.toml"
    assert ConfiguredCaps(config).lock_path == ConfiguredWorkspaces(config).lock_path
    assert ConfiguredCaps(config).lock_path == layout.home / ".config.toml.lock"
    assert ConfiguredCaps(config).lock_path.is_file()
    assert not (layout.home / ".project.lock").exists()


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_program_writes_require_observed_digest(context, operation) -> None:
    before = context.layout.program.read_bytes()
    arguments: dict[str, object] = {"key": "agic:parrot", "if_digest": None}
    if operation != "delete":
        arguments["content"] = {"source": "agic parrot:\n  Changed.\n"}
    result = invoke(context, operation, **arguments)
    assert result.output["error"]["code"] == "invalid_request"
    assert context.layout.program.read_bytes() == before


def test_versions_distinguish_bound_code_from_latest_source(context) -> None:
    original = context.layout.program.read_text()
    bound = sha256(original.encode()).hexdigest()
    context = replace(context, run_program_digest=bound)
    assert invoke(context, "list").output["version"] == {
        "run_digest": bound,
        "authored_digest": bound,
        "matches_run": True,
    }
    latest = original + "\ninstruct goal: New human instruction.\n"
    context.layout.program.write_text(latest)
    read = invoke(context, "get", key="agic:parrot")
    authored = sha256(latest.encode()).hexdigest()
    assert read.output["version"] == {
        "run_digest": bound,
        "authored_digest": authored,
        "matches_run": False,
    }
    stale = invoke(context, "update", content={"source": original}, if_digest=bound)
    assert stale.output["error"]["code"] == "digest_mismatch"
    assert context.layout.program.read_text() == latest
    saved = invoke(
        context,
        "update",
        key="agic:parrot",
        content={"source": "agic parrot:\n  Changed.\n"},
        if_digest=authored,
    )
    assert saved.error is None
    assert "New human instruction." in context.layout.program.read_text()
    assert saved.output["version"]["run_digest"] == bound
    assert saved.output["version"]["matches_run"] is False
    assert saved.output["version"]["authored_digest"] == saved.output["item"]["digest"]


def test_empty_program_still_exposes_version(context) -> None:
    context.layout.program.write_text("")
    assert invoke(context, "list").output == {
        "kind": "program",
        "items": [],
        "version": {
            "run_digest": None,
            "authored_digest": sha256(b"").hexdigest(),
            "matches_run": None,
        },
    }


def test_main_and_flow_writers_serialize_composition(context) -> None:
    def create_flow():
        return asyncio.run(
            create_toolset({})
            .tools()["create"]
            .invoke(
                {
                    "kind": "flow",
                    "key": "research",
                    "content": {"source": "flow:\n  pass\n"},
                },
                context,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        program = pool.submit(
            invoke,
            context,
            "create",
            key="agic:research",
            content={"source": "agic research:\n  Research.\n"},
        )
        flow = pool.submit(create_flow)
        results = [program.result(), flow.result()]
    assert sum(result.error is None for result in results) == 1
    assert (context.home / ".agent.too.lock").is_file()
    assert (context.home / ".flows.lock").is_file()
    assert not (context.home / ".authored-flows.lock").exists()
