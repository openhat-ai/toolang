# Standalone Toolang base package

Status: Proposed; the user confirmed same-repository maintenance, independent
publishing, and legacy forwarding compatibility on 2026-11-07. Implementation
awaits approval of the full definition.

## Goal and scope

Publish the plugin contracts as `toolang-base`. A plugin developer can install
and test against this package without installing Toolang, while Toolang and its
plugins use the same canonical classes and protocols.

Extract the existing `src/toolang/base` modules, including types, protocols,
errors, schemas, money/model-setting helpers, utilities, and examples. They
currently have no imports from other Toolang packages; their only direct
third-party dependency is Pydantic.

Bundled plugins remain in Toolang. Plugin extraction, contract redesign,
execution or stored-format changes, another repository, and publishing releases
are outside scope. The additive development-wheel field below is the only
planned contract extension.

## Package and import ownership

| Item | Decision |
| --- | --- |
| Distribution / import | `toolang-base` / `toolang_base` |
| Location | `packages/toolang-base/src/toolang_base/` |
| Initial SDK version | `0.1.0`, versioned independently of Toolang |
| Runtime requirements | Python `>=3.11`; `pydantic>=2.11,<3` |
| Build | Hatchling; package-owned `pyproject.toml`, README, Apache-2.0 license, and `py.typed` |
| Toolang dependency | `toolang-base>=0.1.0,<0.2.0`; retain Toolang's other directly used dependencies |
| Plugin dependency | The same compatible SDK range plus the plugin's own libraries; Toolang is not required for contract-only plugins |

Each Toolang release declares its supported SDK range; their version numbers
need not match. `uv.lock` fixes development/CI versions, not end-user installations.
SDK `0.1.x` releases must preserve API compatibility. Using a new SDK API raises
Toolang's minimum; adopting incompatible `0.2.x` requires a range update. The
installer combines Toolang/plugin requirements and rejects empty intersections.

Move implementations once; keep the current submodule layout under the new import
name. First-party imports use `toolang_base`. Keep facades narrow, factory
signatures and entry-point groups unchanged, and runtime services/loading
authority in Toolang. The SDK registers no plugins or commands.

Retain `toolang.base.*` modules in Toolang as documented, deprecated forwarding
shims. Forward names in an existing `__all__`, or non-private names where it is
absent. Re-export the actual SDK objects; do not duplicate
definitions, subclass aliases, install import hooks, or split the `toolang`
namespace across distributions. Legacy and canonical imports must preserve class,
exception, and function identity. No import-time warnings are added. Removing
these shims requires a separately approved breaking change.

New external plugins migrate both their imports and dependency metadata together.
Legacy imports still require Toolang to provide the shims; references to
`toolang.common`, execution services, or other runtime modules are not made
standalone by this extraction.

## Development and test ownership

Use a uv workspace with the existing root project and the SDK member, one root
`uv.lock`, and a root workspace source for `toolang-base`. Published metadata
contains the version requirement, without workspace, file, or repository URLs.

Move pure SDK tests from `tests/unit/base` into the member's test directory;
keep runtime-dependent tests in the root suite. Root pytest collection, lint,
type checking, dependency auditing, and CI include both projects; audit exports
omit workspace projects while retaining their third-party dependencies. Add SDK import
boundary checks that run by default and reject imports from `toolang`.

Packaging CI verifies clean installations outside the checkout, without
`PYTHONPATH` or editable installs; workspace tests cannot establish isolation.
Default pytest remains offline and deterministic.

Preserve `--dev [PATH]` syntax and primary Toolang-wheel selection. At the launch
call site, inspect the selected wheel's directory for SDK wheels satisfying its
declared SDK requirement, using `packaging` for metadata/version matching and
declaring it as a direct Toolang dependency: use one compatible companion,
reject multiple matches,
or use ordinary index resolution when there is none. Do not build artifacts
automatically. Pass resolved companion paths through a new
`SandboxRequest.local_dev_dependencies: tuple[Path, ...] = ()` field. Docker stages
and installs these dependencies with the primary wheel using uv's `--with` option.
Include companion paths
and content digests in development-launch identity when present; ordinary launches
retain their existing bindings. CI builds both wheels into one clean directory
when testing unpublished workspace changes in Docker.

## Build and release workflow

- CI checks separate wheels/sdists and rebuilds extracted sdists outside the
  checkout. The SDK sdist is self-contained. Only its wheel owns SDK definitions;
  Toolang's wheel includes forwarding shims. Root sdists may retain workspace
  sources/manifests needed for building.
- Use `--no-sources`, retain Toolang's provenance hook, and use SDK package
  metadata for its version. Keep build/upload directories distribution-specific.
- Keep Toolang tags as `vVERSION`; publish SDK-only releases from
  `toolang-base-vVERSION` through a separate workflow. Each workflow validates
  the selected project's version and publishes only that distribution.
- Filter Toolang source-version resolution and build provenance to its `v*`
  release tags. SDK tags must not change the version reported by `too` or invalidate
  a Toolang release when both tags point to the same commit.
- Extend `too aid.too update_changelog` with `package=toolang|toolang-base`,
  defaulting to Toolang. Select the matching changelog, tag prefix, and release
  links; SDK records live in `packages/toolang-base/CHANGELOG.md`. Preserve existing
  calls and maintainer verification. Before the first SDK tag, require an explicit
  `since` baseline without backfilling monolith release history.
- Publish the required SDK before its dependent Toolang release; require a clean
  registry-based installation as the release gate. A maintainer configures the
  SDK's PyPI project and trusted publisher before its first release.

Implementation build checks use separate temporary output directories, for example:

```sh
uv build --package toolang-base --no-sources --out-dir /tmp/toolang-build/base
uv build --package toolang --no-sources --out-dir /tmp/toolang-build/runtime
```

## Implementation touchpoints

| Area | Likely files |
| --- | --- |
| Package extraction and metadata | `src/toolang/base/**`, `packages/toolang-base/**`, root `pyproject.toml`, `uv.lock` |
| Canonical imports and compatibility | Callers in `src/toolang/**`, `tests/**`, `scripts/benchmark_web_search.py`; legacy base forwarding modules |
| Guest development artifacts | SDK sandbox types, `up/sandbox.py`, `up/contract.py`, Docker sandbox/bootstrap files and their fixtures |
| Versioning and publishing | `common/_version_repo.py`, `common/version.py`, `hatch_build.py`, `.github/workflows/ci.yml`, `publish-pypi.yml`, new SDK publishing workflow, `aid.too` |
| Tests and documentation | Member tests, root compatibility/plugin/package tests, `CONTRIBUTING.md`, relevant design docs, `reference/config.toml`, package README and changelogs |

## Acceptance

1. **SDK-only installation:** wheel and sdist installations import every SDK
   module and run its pure tests with only declared dependencies. `toolang` is
   absent; no CLI, model SDK, server, or plugin registration is installed by base.
2. **Plugin interoperability:** independently built fixtures for all four plugin
   families depend on base alone, load without Toolang, and are accepted by
   Toolang's existing factories. Internal/reserved toolset authority is unchanged.
3. **Compatibility and records:** representative classes, exceptions, and helpers
   are identical through old and new imports; legacy and migrated plugins coexist.
   Existing schema, value-codec, history, and execution round-trip tests pass.
4. **Artifact isolation:** wheels contain disjoint implementations, SDK typing
   metadata is present, and clean Toolang installation resolves the declared SDK
   dependency without repository paths or hidden workspace imports. Compatibility
   smoke checks cover the declared minimum and candidate SDK; conflicting plugin
   requirements fail resolution.
5. **Guest development:** an unpublished compatible SDK companion is installed
   with the selected Toolang wheel; changing it changes launch identity. Ambiguous
   companions fail before launch. Existing primary-only and non-development paths pass.
6. **Independent releases:** SDK tags leave Toolang's version/provenance correct;
   both publishers reject mismatched tags/versions and select only their own
   artifacts. Changelog generation selects the correct project and preserves
   released records.
7. **Repository checks:** default verification and package smoke checks pass on
   supported macOS/Linux and Python versions; no live-provider calls enter the
   default suite. Plugin authoring examples and moved source links are verified.

## Risks and open questions

Shared development environments can conceal missing dependencies; isolated
artifact checks are mandatory. External plugins using private symbols or
runtime modules need owner-led migration beyond the compatibility guarantee.
Third-party guest sandboxes must honor supplied development dependencies to
support local SDK changes.

There are no unresolved implementation choices in this proposal.

References: [current plugin contracts](../design/plugins.md),
[uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/),
[building publishable artifacts](https://docs.astral.sh/uv/guides/package/), and
[PyPA namespace-package guidance](https://packaging.python.org/en/latest/guides/packaging-namespace-packages/).
