# Package Audit

This document defines a repeatable way to audit one Toolang package. The goal
is to find correctness risks, unclear ownership, unnecessary code, dependency
problems, and opportunities to reduce the package without adding forwarding
layers.


## Audit Scope

Start by stating the package's intended responsibility and its stable public
surface. Use design documents and actual callers as evidence; do not infer the
boundary from directory names alone.

Record:

- modules and line counts
- public classes, functions, protocols, and type aliases
- package and subpackage facades
- internal imports and imports from other Toolang packages
- production, test, documentation, and entry-point callers
- tests that directly exercise the package


## Review Order

### 1. Correctness

Inspect code at the package boundary first. Validate serialization,
normalization, type conversion, concurrency, state ownership, and error
handling with representative inputs. A passing test suite does not replace a
manual check for unsupported input shapes or untested runtime conditions.

For plugin-facing packages, verify both sides of every contract:

- protocol and value-type definitions
- factory signatures and plugin loading
- validation at the loading boundary
- behavior under concurrent execution
- durable or streamed representations when applicable

### 2. Dead And Unclaimed Code

Find symbols, modules, compatibility paths, aliases, examples, and re-exports
without production callers. Distinguish among:

- internal dead code, which can be removed
- test fixtures, which should live under `tests/`
- documented public API, which may have external callers
- undocumented public-looking code, whose ownership must be decided

Do not classify a plugin-facing API as dead from repository references alone.
Confirm whether it is documented, exported, covered by contract tests, or
intentionally supported for external packages.

### 3. Ownership And Organization

Check that each module belongs to the package's stated responsibility. Look
for:

- domain logic placed in generic helper modules
- orchestration mixed with value types or protocols
- file parsing outside the package that owns the source format
- persistence outside the package that owns the durable record
- test or demo implementations shipped as runtime code
- classes that only forward arguments without owning state or invariants
- generic `utils`, `manager`, `service`, or `factory` names hiding a concrete
  concept

Prefer a small number of concept-focused modules over one module per class.
Line count alone is not a reason to split a module.

### 4. Common Extraction

Explicitly inspect whether some code should move to `toolang.common`. This can
reduce the audited package while making genuinely shared behavior canonical.

A candidate belongs in `common` only when it:

- has package-neutral semantics
- is used, or is immediately needed, by multiple unrelated packages
- is a leaf dependency and does not import domain packages
- has a small, stable API with independently testable behavior
- removes real duplication or misplaced ownership

Good candidates include small parsing primitives, selector operations,
general formatting or encoding rules, and package-neutral validation helpers.

Do not move code to `common` when it:

- has only one caller
- is shared only inside one owning package
- names or manipulates a domain concept such as runs, caps, jobs, plugins, or
  agent layout
- reads environment variables, configuration, storage, or process state
- coordinates other packages
- merely wraps or re-exports another function
- is moved only to reduce a line count

For each candidate, record its current callers, proposed owner, dependency
direction, and the code that can be deleted after the move. If these are not
clear, leave the code with its current owner.

### 5. Dependencies

Build the package dependency graph and check:

- imports point from orchestration toward stable lower-level concepts
- foundational packages do not import runtime or CLI packages
- no circular imports are hidden by local imports
- optional integrations do not become unconditional core dependencies
- package facades do not create indirect coupling
- callers import canonical owners rather than compatibility facades

Also check dependency weight, import-time work, global mutable state, and
thread or coroutine safety.

### 6. Naming And API Shape

Names should describe the owned concept rather than the implementation
mechanism. Check consistency between class names, method names, persisted
fields, trace fields, and design vocabulary.

Public APIs should:

- accept concept objects instead of ambiguous primitive bundles
- avoid duplicate ways to perform the same operation
- expose stable entry points through a narrow, intentional surface
- keep internal helpers private
- avoid getters, setters, and wrappers that add no semantics

Documentation should state canonical import paths and extension contracts. Do
not add broad re-export facades merely to shorten imports.

### 7. Tests

Map tests to public behavior and important internal invariants. Check for:

- happy paths and malformed inputs
- serialization round trips
- concurrency and cancellation
- plugin contract failures
- persistence and restart behavior
- ordering guarantees
- boundary values and optional fields
- regression tests for every confirmed defect

Run focused package tests during the audit. Run the full required checks before
committing any resulting changes.

### 8. Size And Complexity

Identify the largest modules and the code responsible for their size. Reduce
size by deleting dead code, moving code to its actual owner, consolidating
duplicate behavior, or using an established library when it materially
simplifies validation.

Do not reduce line count through dense formatting, trivial helper functions,
extra facade modules, or abstractions that only move parameters around.


## Evidence

Useful repository checks include:

```text
find src/toolang/<package> -type f
wc -l src/toolang/<package>/**/*.py
rg '^(class|def|async def) ' src/toolang/<package>
rg 'toolang\.<package>' src tests docs pyproject.toml
rg '^(from|import) ' src/toolang/<package>
```

Supplement text searches with representative runtime probes when generated
schemas, serialization, plugin loading, or concurrency behavior cannot be
confirmed by inspection alone.


## Audit Report

Report findings before summaries. Each finding should include:

- priority and user-visible or architectural impact
- exact file and line evidence
- why the current owner or behavior is wrong
- the smallest appropriate correction
- missing regression coverage

Then record:

- package size and largest modules
- dependency direction and any cycles
- code that can be removed or relocated
- `common` candidates and rejected candidates
- focused tests run and remaining risk

An audit is complete when every issue is actionable and ownership is explicit,
not when every module has been made smaller.
