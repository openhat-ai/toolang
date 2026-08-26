# Source Version Provenance

Status: Approved for implementation on 2026-08-26.

## Work Type

Feature implementation of the approved source-version design discussed before
the implementation request.

## Verified Current Behavior

- Distribution metadata and `pyproject.toml` declare package version `0.3.0`.
- `toolang_version()` appends a live `+<revision>*` suffix only when an ancestor
  Git worktree is found.
- An installed wheel inside a repository-local virtual environment can therefore
  be mistaken for development source.
- Wheels do not retain the Git revision or dirty state from which they were
  built.
- The PyPI workflow verifies that `v<package-version>` matches the pushed tag,
  but does not verify embedded source provenance.

## Goal And Success Criteria

Keep the package version used by Python packaging separate from the source
version shown by Toolang. The source version follows native Git describe output
in development and survives sdist and wheel builds.

The change succeeds when:

- editable/source execution displays the native Git describe format without
  requiring the Git executable;
- a built wheel displays the same source version captured at build time,
  including a trailing `*` for a dirty tracked worktree;
- a remote executor reports that same source version through its runtime profile,
  and Chat renders it without altering the version text;
- an sdist carries its captured provenance into a wheel built from that sdist;
- non-editable installs never infer source state from an enclosing Git worktree;
- the package version remains the PEP 440 value from project and distribution
  metadata;
- installation origin is not added to user-facing version text;
- release publication validates a clean exact tag, project version, wheel
  metadata version, and embedded source version;
- source-version resolution is lazy and cached once per process; and
- the default verification passes.

## Version Semantics

The package version remains `0.3.0`-style PEP 440 metadata and controls wheel
filenames and PyPI publication. It is not combined with a revision.

The user-facing Toolang source version uses the format equivalent to:

```sh
git describe --tags --abbrev=8 --dirty='*'
```

Toolang derives it through Dulwich's pure-Python repository API and does not
invoke a Git process. The eight-character minimum keeps revision presentation
stable while embedded build information also retains the full revision.

Examples are:

```text
v0.3.0
v0.3.0*
v0.2.7-87-g3b492a92
v0.2.7-87-g3b492a92*
```

This intentionally uses Git semantics: annotated and lightweight tags are
eligible, the closest reachable tag wins rather than the numerically greatest
tag, and untracked files do not set the dirty suffix. A dirty suffix records
divergence from the commit but does not uniquely identify the changed contents.
If no source version is available, Toolang displays `unknown`.

## Runtime Resolution

Use editable `direct_url.json` metadata, with the existing direct-source
fallback when distribution metadata is absent, only to select development
mode. Development mode reads that repository through Dulwich and never reads
embedded build information. This keeps a long-running process's value as a
startup snapshot after the first lazy, process-cached lookup.

Non-editable execution reads packaged `_build_info.json` and never searches
ancestor directories for `.git`. `direct_url.json` may remain available for
installation diagnostics, but installation origin does not affect or appear in
the source version. CLI and API callers share this resolution through
`toolang.common.version`; no caller independently derives a displayed version.

## Build Provenance

A Hatch custom build hook captures provenance through Dulwich once per standard
artifact build without leaving a generated file in the source checkout. The
packaged schema is:

```json
{
  "schema": 1,
  "source_version": "v0.2.7-87-g3b492a92*",
  "revision": "3b492a92f1ed6282fc5b57a02d091339059cabcf",
  "dirty": true
}
```

`revision` and `dirty` may be `null` when Git metadata is unavailable. An sdist
stores the file at `src/toolang/_build_info.json`; a wheel stores it at
`toolang/_build_info.json`. A wheel built from an sdist validates and reuses the
inherited data instead of describing the extraction environment. Editable
builds do not generate or inject the file.

## Release Validation

For a PyPI release, the workflow requires all of the following:

- the pushed ref is `v<project-version>`;
- the source description equals that tag exactly, which rejects dirty or
  non-exact-tag builds;
- the wheel `METADATA` version equals the project version;
- the wheel's embedded source version equals the pushed tag; and
- the embedded full revision equals the checked-out release commit.

Future release tags should be annotated, while runtime and build logic retain
`--tags` compatibility with existing lightweight tags.

## Scope And Touchpoints

In scope:

- `hatch_build.py` and Hatch build configuration;
- `src/toolang/common/version.py` shared runtime resolution;
- the runtime profile and Chat executor-version presentation;
- deterministic unit tests for runtime and build provenance;
- PyPI workflow release checks; and
- banner examples whose source-version fixture uses the new format.

Out of scope:

- dynamic package metadata or changing wheel filenames;
- display of `dev`, `wheel`, `release`, or another installation origin;
- hashing dirty file contents;
- invoking the Git executable, network or package-index lookup; and
- rewriting existing release tags.

## Acceptance Tests

1. Editable execution returns the mocked native Git describe string and ignores
   embedded build information.
2. Non-editable execution returns valid embedded provenance without repository
   access, including when the installed path has a Git ancestor.
3. Missing or malformed embedded data returns `unknown` safely.
4. Source-version resolution runs once per process through the public helper.
5. Build provenance records describe output, full revision, and dirty state.
6. A build rooted at an sdist reuses valid inherited provenance without
   repository access.
7. A real `uv build` produces an sdist and wheel containing identical build
   information; the wheel retains the package metadata version.
8. Release workflow checks reject tag, source-version, revision, or metadata
   mismatches.
9. A remote runtime profile returns the shared source version, and Chat renders
   it exactly once without adding a prefix.
10. Ruff, formatting, type checking, and the complete default offline suite
    pass.

## Risks And Open Questions

Repository dirty detection can be slower in unusually large or network-mounted
worktrees, but it runs only once at startup or once per artifact build. The
current repository measures roughly 100 ms per uncached Dulwich lookup.
Build-info validation must fail closed for malformed inherited sdists so
provenance is not silently replaced. There are no open questions.
