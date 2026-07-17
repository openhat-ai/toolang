from __future__ import annotations

import ast
from pathlib import Path

from tests import PROJECT_ROOT


SOURCE_ROOT = PROJECT_ROOT / "src" / "toolang"

CATALOG_OWNED_FUNCTIONS = {
    Path("agent/local.py"): frozenset(
        {
            "_clone_local_agent",
            "_default_program_source",
            "write_agent_program",
        }
    ),
    Path("state/caps.py"): frozenset(
        {
            "add_remote_entry",
            "cap_entry_matches_selector",
            "entry_definition_file",
            "entry_form",
            "entry_line",
            "entry_origin",
            "entry_ref",
            "entry_scope",
            "entry_visibility",
            "list_entries",
            "list_local_entries",
            "read_local_entry",
            "remote_entry_name",
            "remove_local_entry",
            "remove_remote_entry",
            "select_cap_entries",
            "split_cap_selectors",
            "write_local_entry",
        }
    ),
    Path("work/definitions.py"): frozenset(
        {
            "_archive_chore",
            "_archive_task",
            "_clone_chore",
            "_clone_task",
            "_create_chore_text",
            "_create_task_text",
            "_draft_chore",
            "_draft_task",
            "_find_archived_chore",
            "_find_archived_task",
            "_find_chore",
            "_find_job",
            "_find_task",
            "_list_archived_chores",
            "_list_archived_tasks",
            "_list_chores",
            "_list_draft_chores",
            "_list_draft_tasks",
            "_list_tasks",
            "_load_chore_text",
            "_load_task_text",
            "_ready_chore",
            "_ready_task",
            "_remove_archived_chore",
            "_remove_archived_task",
            "_save_chore_entry",
            "_save_task_entry",
            "_update_chore_text",
            "_update_task_text",
            "chore_path",
            "job_path",
            "move_chore_lifecycle",
            "move_task_lifecycle",
            "task_path",
        }
    ),
}

CATALOG_BYPASS_CALLS = frozenset(
    {
        "toolang.agent.local.AgentProcess.list",
        "toolang.work.state.AgentJobs.load",
        "toolang.work.state.HomeJobs.load",
    }
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                local_name = imported.asname or imported.name.split(".")[0]
                aliases[local_name] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for imported in node.names:
                if imported.name == "*":
                    continue
                local_name = imported.asname or imported.name
                aliases[local_name] = f"{node.module}.{imported.name}"
    return aliases


def _qualified_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, aliases)
        if parent is not None:
            return f"{parent}.{node.attr}"
    return None


def test_catalog_owned_operations_are_not_implemented_outside_catalog() -> None:
    violations: list[str] = []
    for relative_path, owned_names in CATALOG_OWNED_FUNCTIONS.items():
        path = SOURCE_ROOT / relative_path
        for node in _tree(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in owned_names:
                    violations.append(f"{relative_path}:{node.lineno}: {node.name}")

    assert not violations, (
        "Catalog-owned CRUD and source-resolution operations must be implemented "
        "in toolang.catalog:\n" + "\n".join(violations)
    )


def test_catalog_does_not_call_private_members_of_other_packages() -> None:
    violations: list[str] = []
    for path in sorted((SOURCE_ROOT / "catalog").glob("*.py")):
        tree = _tree(path)
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
                continue
            target = _qualified_name(node.value, aliases)
            if target is None or not target.startswith("toolang."):
                continue
            if target.startswith("toolang.catalog"):
                continue
            relative_path = path.relative_to(SOURCE_ROOT)
            violations.append(
                f"{relative_path}:{node.lineno}: {target}.{node.attr}"
            )

    assert not violations, (
        "Catalog must implement its operations instead of forwarding to private "
        "members of another package:\n" + "\n".join(violations)
    )


def test_catalog_operations_are_not_bypassed_by_callers() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = _tree(path)
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _qualified_name(node.func, aliases)
            if target not in CATALOG_BYPASS_CALLS:
                continue
            relative_path = path.relative_to(SOURCE_ROOT)
            violations.append(f"{relative_path}:{node.lineno}: {target}")

    assert not violations, (
        "Callers must use catalog operations instead of loading authored state or "
        "enumerating agents directly:\n" + "\n".join(violations)
    )
