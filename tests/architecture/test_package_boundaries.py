from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

from tests import PROJECT_ROOT


SOURCE_ROOT = PROJECT_ROOT / "src" / "toolang"

PACKAGES = (
    "api",
    "base",
    "catalog",
    "cli",
    "common",
    "execution",
    "up",
    "lang",
    "plugin",
    "setup",
    "state",
    "work",
)

PACKAGE_IMPORT_RULES: dict[str, frozenset[str] | None] = {
    "api": None,  # TODO: Review the API package boundary.
    "base": frozenset(),
    "catalog": frozenset({"common"}),
    "cli": None,  # TODO: Review the CLI package boundary.
    "common": frozenset({"base"}),
    "execution": None,  # TODO: Review the execution package boundary.
    # Up is the top-level process composition boundary. It may assemble
    # the API, concrete catalogs, and runtime owners, but it must never depend
    # on CLI orchestration or language implementation details.
    "up": frozenset(
        {
            "api",
            "base",
            "catalog",
            "common",
            "execution",
            "plugin",
            "setup",
            "state",
            "work",
        }
    ),
    # lang uses the shared error type and immutable metadata containers.
    "lang": frozenset({"base", "common"}),
    # common is currently needed only for shared collection-query behavior.
    "plugin": frozenset({"base", "common"}),
    "setup": frozenset({"base", "common", "plugin"}),
    "state": None,  # TODO: Review the state package boundary.
    "work": None,  # TODO: Review the work package boundary.
}


def _source_packages() -> frozenset[str]:
    return frozenset(path.parent.name for path in SOURCE_ROOT.glob("*/__init__.py"))


def _module_context(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    return ".".join(("toolang", *relative.parts[:-1]))


def _import_targets(node: ast.Import | ast.ImportFrom, context: str) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(imported.name for imported in node.names)

    if node.level:
        module = resolve_name("." * node.level + (node.module or ""), context)
    else:
        module = node.module or ""
    if module != "toolang":
        return (module,)
    return (module, *(f"toolang.{imported.name}" for imported in node.names))


def _package_imports(package: str) -> dict[str, tuple[str, ...]]:
    references: dict[str, list[str]] = {}
    known_packages = frozenset(PACKAGES)
    for path in sorted((SOURCE_ROOT / package).rglob("*.py")):
        context = _module_context(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, context):
                if not target.startswith("toolang."):
                    continue
                imported_package = target.split(".", 2)[1]
                if (
                    imported_package == package
                    or imported_package not in known_packages
                ):
                    continue
                reference = f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}"
                references.setdefault(imported_package, []).append(reference)
    return {name: tuple(paths) for name, paths in references.items()}


@pytest.mark.parametrize("package", PACKAGES, ids=lambda name: f"toolang.{name}")
def test_package_imports_are_allowed(package: str) -> None:
    allowed_imports = PACKAGE_IMPORT_RULES[package]
    if allowed_imports is None:
        pytest.skip("package import boundary pending review")

    imports = _package_imports(package)
    unexpected = sorted(imports.keys() - allowed_imports)
    details = "\n".join(
        f"toolang.{package} -> toolang.{dependency}: {', '.join(imports[dependency])}"
        for dependency in unexpected
    )

    assert not unexpected, f"Unexpected internal package imports:\n{details}"


def test_package_boundary_coverage() -> None:
    declared_packages = frozenset(PACKAGES)
    source_packages = _source_packages()
    rule_packages = frozenset(PACKAGE_IMPORT_RULES)
    reviewed_rules = {
        package: allowed
        for package, allowed in PACKAGE_IMPORT_RULES.items()
        if allowed is not None
    }
    allowed_targets = frozenset().union(*reviewed_rules.values())
    self_import_rules = sorted(
        package for package, allowed in reviewed_rules.items() if package in allowed
    )

    assert declared_packages == source_packages, (
        "PACKAGES must cover every top-level source package; "
        f"missing={sorted(source_packages - declared_packages)}, "
        f"unknown={sorted(declared_packages - source_packages)}"
    )
    assert rule_packages == declared_packages, (
        "Every package must have one import rule; "
        f"missing={sorted(declared_packages - rule_packages)}, "
        f"unknown={sorted(rule_packages - declared_packages)}"
    )
    assert allowed_targets <= declared_packages, (
        "Import rules may only name declared internal packages; "
        f"unknown={sorted(allowed_targets - declared_packages)}"
    )
    assert not self_import_rules, (
        "Import rules only declare dependencies on other packages; "
        f"self_imports={self_import_rules}"
    )


def test_schema_modules_do_not_depend_on_runtime_services() -> None:
    forbidden_modules = frozenset(
        {
            "executor",
            "history",
            "inspection",
            "manager",
            "server",
            "sink",
            "store",
            "watcher",
        }
    )
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("schemas.py")):
        context = _module_context(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, context):
                if not target.startswith("toolang."):
                    continue
                if target.rsplit(".", 1)[-1] not in forbidden_modules:
                    continue
                violations.append(
                    f"{path.relative_to(SOURCE_ROOT)}:{node.lineno} -> {target}"
                )

    assert not violations, (
        "Schema modules must not import stores, watchers, or runtime services:\n"
        + "\n".join(violations)
    )


def test_runtime_toolset_depends_only_on_base_contracts() -> None:
    violations: list[str] = []
    path = SOURCE_ROOT / "execution" / "tools" / "_toolang.py"
    context = _module_context(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _import_targets(node, context):
            if target != "toolang" and not target.startswith("toolang."):
                continue
            if target == "toolang.base" or target.startswith("toolang.base."):
                continue
            violations.append(
                f"{path.relative_to(SOURCE_ROOT)}:{node.lineno} -> {target}"
            )

    assert not violations, (
        "The runtime toolset must use base protocols, not concrete runtime owners:\n"
        + "\n".join(violations)
    )


def test_primitive_execution_inspection_does_not_depend_on_trees() -> None:
    violations: list[str] = []
    for name in (
        "inspection/__init__.py",
        "inspection/types.py",
        "inspection/views.py",
        "store.py",
    ):
        path = SOURCE_ROOT / "execution" / name
        context = _module_context(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, context):
                if target == "toolang.execution.inspection.trees":
                    violations.append(f"{name}:{node.lineno}")

    assert not violations, (
        "Primitive inspection must remain usable without tree projection: "
        + ", ".join(violations)
    )


@pytest.mark.parametrize(
    "name",
    [
        "records.py",
        "inspection/__init__.py",
        "inspection/types.py",
        "inspection/views.py",
    ],
)
def test_execution_record_and_view_types_have_no_runtime_dependencies(
    name: str,
) -> None:
    allowed = {
        "toolang.execution.types",
        "toolang.execution.records",
        "toolang.execution.inspection.types",
    }
    path = SOURCE_ROOT / "execution" / name
    context = _module_context(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _import_targets(node, context):
            if target.startswith("toolang.execution.") and target not in allowed:
                violations.append(f"{name}:{node.lineno} -> {target}")

    assert not violations, (
        "Record codecs and inspection primitives must not import message rendering, "
        "queries, or runtime owners:\n" + "\n".join(violations)
    )


def test_model_call_assembly_does_not_depend_on_runtime_owners() -> None:
    allowed = {
        "toolang.execution.records",
        "toolang.execution.types",
        "toolang.execution.recall",
        "toolang.execution.values",
    }
    violations: list[str] = []
    for path in sorted((SOURCE_ROOT / "execution" / "assembly").rglob("*.py")):
        context = _module_context(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, context):
                if (
                    not target.startswith("toolang.execution.")
                    or target == "toolang.execution.assembly"
                    or target.startswith("toolang.execution.assembly.")
                    or target in allowed
                ):
                    continue
                violations.append(
                    f"{path.relative_to(SOURCE_ROOT)}:{node.lineno} -> {target}"
                )

    assert not violations, (
        "Model-call assembly must consume data without importing runtime owners:\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "module,allowed",
    [
        ("prompting", {"messages", "utils", "prompts"}),
        ("messages", {"tool_replies", "utils"}),
        ("tool_replies", set()),
        ("utils", set()),
    ],
)
def test_assembly_modules_have_one_way_dependencies(module, allowed) -> None:
    package = "toolang.execution.assembly"
    path = SOURCE_ROOT / "execution" / "assembly" / f"{module}.py"
    context = _module_context(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    dependencies = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _import_targets(node, context):
            if target == package and isinstance(node, ast.ImportFrom):
                dependencies.update(alias.name for alias in node.names)
            elif target.startswith(package + "."):
                dependencies.add(target.removeprefix(package + ".").split(".")[0])

    assert dependencies <= allowed, (
        f"{module} has misplaced dependencies: {dependencies - allowed}"
    )


def test_assembly_utils_depend_only_on_message_vocabulary() -> None:
    allowed = {"toolang.base.types.message", "toolang.execution.types"}
    path = SOURCE_ROOT / "execution" / "assembly" / "utils.py"
    context = _module_context(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _import_targets(node, context):
            if target.startswith("toolang.") and target not in allowed:
                violations.append(f"{node.lineno} -> {target}")

    assert not violations, (
        "Assembly utilities must not depend on prompts, history, or runtime owners:\n"
        + "\n".join(violations)
    )


def test_external_click_is_confined_to_the_editor() -> None:
    editor = SOURCE_ROOT / "cli" / "common" / "editor.py"
    violations: list[str] = []
    for root in (SOURCE_ROOT, PROJECT_ROOT / "tests"):
        for path in sorted(root.rglob("*.py")):
            if path == editor:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    targets = [node.module or ""]
                else:
                    continue
                if any(target.split(".")[0] == "click" for target in targets):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert not violations, (
        "Use Typer outside external editor integration:\n" + "\n".join(violations)
    )
