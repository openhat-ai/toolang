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
    # common is currently needed only for shared selector parsing and matching.
    "plugin": frozenset({"base", "common"}),
    "setup": frozenset({"base", "common", "plugin"}),
    "state": None,  # TODO: Review the state package boundary.
    "work": None,  # TODO: Review the work package boundary.
}


def _source_packages() -> frozenset[str]:
    return frozenset(
        path.parent.name for path in SOURCE_ROOT.glob("*/__init__.py")
    )


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
                if imported_package == package or imported_package not in known_packages:
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
        package
        for package, allowed in reviewed_rules.items()
        if package in allowed
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
