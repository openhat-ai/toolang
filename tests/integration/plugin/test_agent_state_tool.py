from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from toolang.base.errors import ToolFailure
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.tool import ToolContext
from toolang.catalog import cap as caps
from toolang.catalog.job import AuthoredJobs
from toolang.execution.tools.agent_state import create_toolset


def _context(toolang_root: Path, agent_name: str = "alice") -> ToolContext:
    home = toolang_root / "agents" / agent_name
    home.mkdir(parents=True, exist_ok=True)
    (home / "agent.too").write_text(f"agent {agent_name}\n", encoding="utf-8")
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / "_me",
        wd=home,
    )


def _invoke(
    tool: AgentTool,
    arguments: dict[str, object],
    context: ToolContext,
) -> dict[str, Any]:
    return asyncio.run(tool.invoke(arguments, context))


def _tools() -> dict[str, AgentTool]:
    return dict(create_toolset({}).tools())


def _error(
    tool: AgentTool,
    arguments: dict[str, object],
    context: ToolContext,
) -> dict[str, Any]:
    with pytest.raises(ToolFailure) as raised:
        _invoke(tool, arguments, context)
    return raised.value.output["error"]


def test_compact_toolset_exposes_five_closed_schemas() -> None:
    tools = _tools()

    assert tuple(tools) == ("list", "get", "create", "update", "delete")
    for name, tool in tools.items():
        schema = tool.definition().parameters
        assert schema["additionalProperties"] is False
        properties = schema["properties"]
        assert {"agent", "agent_name", "home", "root", "path", "scope"}.isdisjoint(
            properties
        )
        kinds = properties["kind"]["enum"]
        if name == "delete":
            assert kinds == ["psyche", "skill", "service", "prompt", "flow"]
        else:
            assert kinds == [
                "task",
                "chore",
                "psyche",
                "skill",
                "service",
                "prompt",
                "flow",
            ]


def test_compact_tools_bound_invalid_identifiers_and_error_fields(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path / "toolang")
    long_key = "k" * 1_000
    long_field = "field_" + "x" * 1_000

    key_error = _error(
        _tools()["get"],
        {"kind": "prompt", "key": long_key},
        context,
    )
    field_error = _error(
        _tools()["create"],
        {
            "kind": "task",
            "content": {"body": "Keep this bounded.", long_field: "value"},
        },
        context,
    )

    assert key_error["code"] == "invalid_request"
    assert len(key_error["key"]) == 128
    assert len(field_error["issues"][0]["path"]) == 256
    assert len(str(field_error)) < 2_000


def test_compact_tools_create_list_get_and_update_ready_jobs(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    context = _context(root)
    tools = _tools()

    task = _invoke(
        tools["create"],
        {
            "kind": "task",
            "content": {"title": "Review plan", "body": "Review it."},
        },
        context,
    )["item"]
    chore = _invoke(
        tools["create"],
        {
            "kind": "chore",
            "content": {
                "title": "Check PRs",
                "body": "Report stale PRs.",
                "schedule": "FREQ=HOURLY;INTERVAL=6",
            },
        },
        context,
    )["item"]

    listed = _invoke(tools["list"], {"kind": "task"}, context)
    loaded = _invoke(tools["get"], {"kind": "task", "key": task["key"]}, context)
    updated = _invoke(
        tools["update"],
        {
            "kind": "chore",
            "key": chore["key"],
            "content": {
                "schedule": "FREQ=DAILY;INTERVAL=1",
                "body": "Report blockers.",
            },
        },
        context,
    )

    assert listed["items"][0]["key"] == task["key"]
    assert loaded["item"]["content"] == {
        "title": "Review plan",
        "body": "Review it.",
    }
    assert updated["changed"] is True
    assert updated["item"]["content"]["schedule"] == "FREQ=DAILY;INTERVAL=1"
    assert updated["item"]["content"]["body"] == "Report blockers."
    assert all(not Path(item["path"]).is_absolute() for item in listed["items"])

    invalid_schedule = _error(
        tools["create"],
        {
            "kind": "chore",
            "content": {"body": "Never write this.", "schedule": "not-an-rrule"},
        },
        context,
    )
    assert invalid_schedule["code"] == "invalid_content"
    assert len(_invoke(tools["list"], {"kind": "chore"}, context)["items"]) == 1


def test_invalid_job_content_does_not_allocate_an_id(tmp_path: Path) -> None:
    context = _context(tmp_path / "toolang")

    error = _error(
        _tools()["create"],
        {
            "kind": "chore",
            "content": {"body": "Invalid.", "schedule": "not-an-rrule"},
        },
        context,
    )

    assert error["code"] == "invalid_content"
    assert not context.home.joinpath(".runtime", "ids.json").exists()
    assert not context.home.joinpath("chores").exists()


def test_compact_tools_report_invalid_authored_frontmatter_without_echoing_it(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path / "toolang")
    prompt_path = context.home / "prompts" / "broken.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text(
        "---\nprivate_marker: [\n---\nDo not echo this.\n",
        encoding="utf-8",
    )
    task_path = context.home / "tasks" / "broken.md"
    task_path.parent.mkdir()
    task_path.write_text(
        "---\nprivate_marker: [\n---\nDo not echo this either.\n",
        encoding="utf-8",
    )

    prompt_error = _error(
        _tools()["get"],
        {"kind": "prompt", "key": "broken"},
        context,
    )
    task_error = _error(_tools()["list"], {"kind": "task"}, context)

    assert prompt_error["code"] == "invalid_content"
    assert prompt_error["issues"] == [
        {
            "code": "invalid-frontmatter",
            "path": "key",
            "message": "authored front matter is invalid",
        }
    ]
    assert task_error["code"] == "invalid_content"
    assert task_error["issues"][0]["path"] == "kind"
    assert "private_marker" not in str(prompt_error)
    assert "private_marker" not in str(task_error)


def test_compact_job_conflicts_do_not_expose_storage_paths(tmp_path: Path) -> None:
    context = _context(tmp_path / "toolang")
    tasks = context.home / "tasks"
    tasks.mkdir()
    content = "---\nid: duplicate\n---\nDo work.\n"
    tasks.joinpath("one.md").write_text(content, encoding="utf-8")
    tasks.joinpath("two.md").write_text(content, encoding="utf-8")

    error = _error(_tools()["list"], {"kind": "task"}, context)

    assert error["code"] == "conflict"
    assert error["message"] == "authored task conflicts with existing content"
    assert str(tmp_path) not in str(error)


def test_compact_job_tools_use_only_ready_documents(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    context = _context(root)
    tools = _tools()
    catalog = AuthoredJobs(context.home)
    created = _invoke(
        tools["create"],
        {"kind": "task", "content": {"body": "Archive me."}},
        context,
    )["item"]
    catalog.move("task", created["key"], "archived")

    assert _invoke(tools["list"], {"kind": "task"}, context)["items"] == []
    error = _error(tools["get"], {"kind": "task", "key": created["key"]}, context)

    assert error["code"] == "not_found"


def test_compact_cap_tools_round_trip_all_content_and_stay_home_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    context = _context(root)
    tools = _tools()
    root_caps = caps.AuthoredCaps(root)
    root_caps.create(caps.CapFile.parse("Root.\n", kind="psyche", name="shared"))

    assert _invoke(tools["list"], {"kind": "psyche"}, context)["items"] == []
    created = _invoke(
        tools["create"],
        {"kind": "psyche", "key": "shared", "content": {"body": "Home."}},
        context,
    )
    skill = _invoke(
        tools["create"],
        {
            "kind": "skill",
            "key": "reviewer",
            "content": {"description": "Review code.", "body": "Check tests."},
        },
        context,
    )
    service = _invoke(
        tools["create"],
        {
            "kind": "service",
            "key": "search",
            "content": {
                "description": "Search service.",
                "transport": "http",
                "target": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer $TOKEN"},
                "env": ["TOKEN"],
            },
        },
        context,
    )
    prompt = _invoke(
        tools["create"],
        {
            "kind": "prompt",
            "key": "summarize",
            "content": {"body": "Summarize: {{input}}"},
        },
        context,
    )

    assert created["item"]["content"] == {"body": "Home."}
    assert skill["item"]["content"]["description"] == "Review code."
    assert service["item"]["content"]["headers"] == {"Authorization": "Bearer $TOKEN"}
    assert prompt["item"]["content"] == {"body": "Summarize: {{input}}"}
    assert root_caps.get("psyche", "shared") is not None

    invalid_service = _error(
        tools["create"],
        {
            "kind": "service",
            "key": "broken",
            "content": {
                "description": "Broken.",
                "transport": "pipe",
                "target": "command",
            },
        },
        context,
    )
    assert invalid_service["code"] == "invalid_content"
    assert not context.home.joinpath("services", "broken.md").exists()

    deleted = _invoke(tools["delete"], {"kind": "skill", "key": "reviewer"}, context)
    assert deleted == {"kind": "skill", "key": "reviewer", "deleted": True}
    assert not (context.home / "skills" / "reviewer").exists()


def test_compact_service_update_replaces_legacy_protocol_metadata(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path / "toolang")
    path = context.home / "services" / "legacy.md"
    path.parent.mkdir()
    path.write_text(
        "---\n"
        "description: Legacy service.\n"
        "protocol: stdio\n"
        "target: legacy-command\n"
        "---\n"
        "Legacy.\n",
        encoding="utf-8",
    )

    updated = _invoke(
        _tools()["update"],
        {
            "kind": "service",
            "key": "legacy",
            "content": {"transport": "http", "target": "https://example.com/mcp"},
        },
        context,
    )
    metadata = frontmatter.load(path).metadata

    assert updated["changed"] is True
    assert metadata["transport"] == "http"
    assert "protocol" not in metadata


def test_compact_update_supports_digest_preconditions_and_noop(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    context = _context(root)
    tools = _tools()
    created = _invoke(
        tools["create"],
        {
            "kind": "prompt",
            "key": "note",
            "content": {"body": "First."},
        },
        context,
    )["item"]
    path = context.home / created["path"]

    mismatch = _error(
        tools["update"],
        {
            "kind": "prompt",
            "key": "note",
            "content": {"body": "Second."},
            "if_digest": "0" * 64,
        },
        context,
    )
    unchanged = path.read_text(encoding="utf-8")
    noop = _invoke(
        tools["update"],
        {
            "kind": "prompt",
            "key": "note",
            "content": {"body": "First."},
            "if_digest": created["digest"],
        },
        context,
    )

    assert mismatch["code"] == "digest_mismatch"
    assert path.read_text(encoding="utf-8") == unchanged
    assert noop["changed"] is False


def test_compact_mutations_check_digests_for_every_supported_kind(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path / "toolang")
    tools = _tools()
    cases = (
        ("task", None, {"body": "Task."}, {"body": "Task."}),
        (
            "chore",
            None,
            {"body": "Chore.", "schedule": "FREQ=DAILY;INTERVAL=1"},
            {"body": "Chore."},
        ),
        ("psyche", "calm", {"body": "Calm."}, {"body": "Calm."}),
        (
            "skill",
            "review",
            {"description": "Review.", "body": "Review."},
            {"description": "Review."},
        ),
        (
            "service",
            "search",
            {
                "description": "Search.",
                "transport": "http",
                "target": "https://example.com/mcp",
            },
            {"target": "https://example.com/mcp"},
        ),
        ("prompt", "note", {"body": "Note."}, {"body": "Note."}),
        (
            "flow",
            "research",
            {"source": "flow:\n  pass\n"},
            {"source": "flow:\n  pass\n"},
        ),
    )

    for kind, requested_key, create_content, update_content in cases:
        arguments: dict[str, object] = {
            "kind": kind,
            "content": create_content,
        }
        if requested_key is not None:
            arguments["key"] = requested_key
        item = _invoke(tools["create"], arguments, context)["item"]
        path = context.home / item["path"]
        before = path.read_bytes()

        mismatch = _error(
            tools["update"],
            {
                "kind": kind,
                "key": item["key"],
                "content": update_content,
                "if_digest": "0" * 64,
            },
            context,
        )
        noop = _invoke(
            tools["update"],
            {
                "kind": kind,
                "key": item["key"],
                "content": update_content,
                "if_digest": item["digest"],
            },
            context,
        )

        assert mismatch["code"] == "digest_mismatch"
        assert path.read_bytes() == before
        assert noop["changed"] is False

        if kind not in {"task", "chore"}:
            delete_mismatch = _error(
                tools["delete"],
                {"kind": kind, "key": item["key"], "if_digest": "0" * 64},
                context,
            )
            assert delete_mismatch["code"] == "digest_mismatch"
            assert path.read_bytes() == before


def test_compact_runtime_validation_returns_structured_errors(tmp_path: Path) -> None:
    context = _context(tmp_path / "toolang")
    tools = _tools()

    foreign = _error(
        tools["create"],
        {"kind": "skill", "key": "review", "content": {"source": "flow:"}},
        context,
    )
    job_key = _error(
        tools["create"],
        {"kind": "task", "key": "manual", "content": {"body": "Task."}},
        context,
    )
    job_delete = _error(tools["delete"], {"kind": "task", "key": "task-1"}, context)
    path_key = _error(tools["get"], {"kind": "prompt", "key": "../root"}, context)
    flow_key = _error(tools["get"], {"kind": "flow", "key": "not portable"}, context)
    unknown_kind = _error(tools["list"], {"kind": "unknown"}, context)
    missing_key = _error(
        tools["create"], {"kind": "prompt", "content": {"body": "Hi."}}, context
    )
    empty_update = _error(
        tools["update"],
        {"kind": "skill", "key": "review", "content": {}},
        context,
    )
    bad_digest = _error(
        tools["update"],
        {
            "kind": "prompt",
            "key": "note",
            "content": {"body": "Hi."},
            "if_digest": "ABC",
        },
        context,
    )
    many_issues = _error(
        tools["create"],
        {
            "kind": "psyche",
            "key": "busy",
            "content": {"body": "Hi.", **{f"extra{index}": "x" for index in range(40)}},
        },
        context,
    )

    assert foreign["code"] == "invalid_content"
    assert foreign["issues"][0]["path"] == "content.source"
    assert job_key["code"] == "invalid_request"
    assert job_delete["code"] == "unsupported_operation"
    assert path_key["code"] == "invalid_request"
    assert path_key["issues"][0]["path"] == "key"
    assert flow_key["code"] == "invalid_request"
    assert flow_key["issues"][0]["path"] == "key"
    assert unknown_kind["code"] == "invalid_request"
    assert missing_key["code"] == "invalid_request"
    assert empty_update["code"] == "invalid_content"
    assert bad_digest["code"] == "invalid_request"
    assert len(many_issues["issues"]) == 32
    assert many_issues["truncated"] is True


def test_compact_flow_tools_validate_before_atomic_mutation(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    context = _context(root)
    tools = _tools()

    invalid = _error(
        tools["create"],
        {
            "kind": "flow",
            "key": "research",
            "content": {"source": "flow research:\n  settle missing\n"},
        },
        context,
    )
    target = context.home / "flows" / "research.too"
    assert invalid["code"] == "invalid_flow"
    assert invalid["issues"][0]["path"] == "content.source"
    assert not target.exists()

    created = _invoke(
        tools["create"],
        {
            "kind": "flow",
            "key": "research",
            "content": {"source": "flow:\n  pass\n"},
        },
        context,
    )["item"]
    listed = _invoke(tools["list"], {"kind": "flow"}, context)
    loaded = _invoke(tools["get"], {"kind": "flow", "key": "research"}, context)

    assert listed["items"] == [
        {
            "key": "research",
            "path": "flows/research.too",
            "digest": created["digest"],
            "bytes": len("flow:\n  pass\n"),
        }
    ]
    assert loaded["item"]["content"] == {"source": "flow:\n  pass\n"}

    failed_update = _error(
        tools["update"],
        {
            "kind": "flow",
            "key": "research",
            "content": {"source": "flow other:\n  pass\n"},
            "if_digest": created["digest"],
        },
        context,
    )
    assert failed_update["code"] == "invalid_flow"
    assert target.read_text(encoding="utf-8") == "flow:\n  pass\n"

    deleted = _invoke(
        tools["delete"],
        {"kind": "flow", "key": "research", "if_digest": created["digest"]},
        context,
    )
    assert deleted == {"kind": "flow", "key": "research", "deleted": True}
    assert not target.exists()


def test_compact_flow_create_rejects_public_runnable_conflict(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    context = _context(root)
    context.home.joinpath("agent.too").write_text(
        "agent alice\n\nflow research:\n  pass\n", encoding="utf-8"
    )
    error = _error(
        _tools()["create"],
        {
            "kind": "flow",
            "key": "research",
            "content": {"source": "flow:\n  pass\n"},
        },
        context,
    )

    assert error["code"] == "invalid_flow"
    assert error["issues"][0]["code"] == "public-runnable-conflict"
    assert not (context.home / "flows" / "research.too").exists()


def test_compact_flow_create_accepts_named_export_and_rejects_case_collision(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path / "toolang")
    tools = _tools()

    named = _invoke(
        tools["create"],
        {
            "kind": "flow",
            "key": "analysis",
            "content": {"source": "flow analysis:\n  pass\n"},
        },
        context,
    )
    unnamed = _invoke(
        tools["create"],
        {
            "kind": "flow",
            "key": "Research",
            "content": {"source": "flow:\n  pass\n"},
        },
        context,
    )
    collision = _error(
        tools["create"],
        {
            "kind": "flow",
            "key": "research",
            "content": {"source": "flow research:\n  pass\n"},
        },
        context,
    )
    wrong_case = _error(tools["get"], {"kind": "flow", "key": "research"}, context)

    assert named["created"] is True
    assert unnamed["created"] is True
    assert collision["code"] == "conflict"
    assert wrong_case["code"] == "not_found"
    assert {path.name for path in context.home.joinpath("flows").iterdir()} == {
        "Research.too",
        "analysis.too",
    }


def test_compact_flow_tools_reject_symlinked_storage(tmp_path: Path) -> None:
    context = _context(tmp_path / "toolang")
    external = tmp_path / "external"
    external.mkdir()
    external.joinpath("research.too").write_text(
        "flow research:\n  pass\n", encoding="utf-8"
    )
    context.home.joinpath("flows").symlink_to(external, target_is_directory=True)

    error = _error(_tools()["list"], {"kind": "flow"}, context)

    assert error["code"] == "storage_error"
    assert external.joinpath("research.too").is_file()

    context.home.joinpath("flows").unlink()
    context.home.joinpath("flows").mkdir()
    context.home.joinpath("flows", "linked.too").symlink_to(external / "research.too")
    create_error = _error(
        _tools()["create"],
        {
            "kind": "flow",
            "key": "new_flow",
            "content": {"source": "flow:\n  pass\n"},
        },
        context,
    )

    assert create_error["code"] == "storage_error"
    assert not context.home.joinpath("flows", "new_flow.too").exists()


def test_compact_flow_update_and_delete_can_recover_invalid_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    context = _context(root)
    updated_path = context.home / "flows" / "updated.too"
    updated_path.parent.mkdir()
    updated_path.write_bytes(b"\xff")

    updated = _invoke(
        _tools()["update"],
        {
            "kind": "flow",
            "key": "updated",
            "content": {"source": "flow:\n  pass\n"},
        },
        context,
    )

    deleted_path = context.home / "flows" / "deleted.too"
    deleted_path.write_bytes(b"\xff")

    deleted = _invoke(_tools()["delete"], {"kind": "flow", "key": "deleted"}, context)

    assert updated["changed"] is True
    assert updated_path.read_text(encoding="utf-8") == "flow:\n  pass\n"
    assert deleted["deleted"] is True
    assert not deleted_path.exists()


def test_compact_tools_reject_symlinked_cap_job_and_runtime_storage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"

    cap_context = _context(root, "caps")
    external_caps = tmp_path / "external-caps"
    external_caps.mkdir()
    external_caps.joinpath("secret.md").write_text("Secret.\n", encoding="utf-8")
    cap_context.home.joinpath("psyches").symlink_to(
        external_caps,
        target_is_directory=True,
    )
    cap_error = _error(_tools()["list"], {"kind": "psyche"}, cap_context)
    flow_created = _invoke(
        _tools()["create"],
        {
            "kind": "flow",
            "key": "isolated",
            "content": {"source": "flow:\n  pass\n"},
        },
        cap_context,
    )

    job_context = _context(root, "jobs")
    external_jobs = tmp_path / "external-jobs"
    external_jobs.mkdir()
    job_context.home.joinpath("tasks").symlink_to(
        external_jobs,
        target_is_directory=True,
    )
    job_error = _error(_tools()["list"], {"kind": "task"}, job_context)

    runtime_context = _context(root, "runtime")
    external_runtime = tmp_path / "external-runtime"
    external_runtime.mkdir()
    runtime_context.home.joinpath(".runtime").symlink_to(
        external_runtime,
        target_is_directory=True,
    )
    runtime_error = _error(
        _tools()["create"],
        {"kind": "task", "content": {"body": "Do not create."}},
        runtime_context,
    )

    assert cap_error["code"] == "storage_error"
    assert flow_created["created"] is True
    assert job_error["code"] == "storage_error"
    assert runtime_error["code"] == "storage_error"
    assert external_caps.joinpath("secret.md").read_text(encoding="utf-8") == (
        "Secret.\n"
    )
    assert list(external_jobs.iterdir()) == []
    assert list(external_runtime.iterdir()) == []


def test_compact_skill_delete_rejects_nested_symlinks(tmp_path: Path) -> None:
    context = _context(tmp_path / "toolang")
    tools = _tools()
    _invoke(
        tools["create"],
        {
            "kind": "skill",
            "key": "review",
            "content": {"description": "Review.", "body": "Review code."},
        },
        context,
    )
    external = tmp_path / "external.txt"
    external.write_text("Keep.\n", encoding="utf-8")
    context.home.joinpath("skills", "review", "external.txt").symlink_to(external)

    error = _error(tools["delete"], {"kind": "skill", "key": "review"}, context)

    assert error["code"] == "storage_error"
    assert context.home.joinpath("skills", "review", "SKILL.md").is_file()
    assert external.read_text(encoding="utf-8") == "Keep.\n"


def test_compact_tool_rejects_symlinked_agent_home(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    target = root / "agents" / "bob"
    target.mkdir(parents=True)
    target.joinpath("agent.too").write_text("agent bob\n", encoding="utf-8")
    home = root / "agents" / "alice"
    home.symlink_to(target, target_is_directory=True)
    context = ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / "_me",
        wd=home,
    )

    error = _error(_tools()["list"], {"kind": "task"}, context)

    assert error["code"] == "invalid_request"


def test_compact_tool_rejects_non_agent_home_with_structured_error(
    tmp_path: Path,
) -> None:
    context = ToolContext(
        run_id="run-1",
        home=tmp_path / "alice",
        room=tmp_path / "alice" / ".runtime" / "tools" / "_me",
        wd=tmp_path / "alice",
    )

    error = _error(_tools()["list"], {"kind": "task"}, context)

    assert error["code"] == "invalid_request"
