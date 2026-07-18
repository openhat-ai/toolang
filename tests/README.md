# Test Suite Organization

The test suite is grouped first by test scope and then by the Toolang boundary
or user-facing surface under test.

## Scopes

- `unit/` tests one package-owned concept or component. Cheap local resources,
  such as a temporary file or SQLite database, do not make a test an
  integration test when that resource is part of the concept's behavior.
- `integration/` verifies assembly across package boundaries, such as catalog
  and work state, execution and persistence, or CLI and runtime orchestration.
- `architecture/` statically enforces ownership, dependency direction, and
  package boundaries.
- `system/` exercises installed entry points or process-level behavior.
- `support/` contains shared test builders. It is not collected as a test
  scope.
- `fixtures/` contains source documents and other static test data.

Within a scope, directories mirror the owning package or the public surface
being exercised. New test modules should cover one coherent subject instead
of accumulating in a generic package-wide module.

## Running Tests

Run the complete suite by default:

```console
uv run pytest -q
```

Run one scope while developing:

```console
uv run pytest -q tests/unit
uv run pytest -q tests/integration
uv run pytest -q tests/architecture
uv run pytest -q tests/system
```

Pytest markers should describe execution requirements such as network,
Docker, or unusually slow behavior. Do not duplicate the directory scopes
with markers.
