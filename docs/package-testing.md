# Package Testing

This document defines how to evaluate and improve test coverage for one
Toolang package. The goal is confidence in the package's behavior and
boundaries, not a high line-coverage number by itself.


## Testing Scope

Begin with the package responsibility, public API, and direct callers. Build an
inventory of:

- public classes, functions, protocols, and type aliases
- persisted or streamed data shapes
- state transitions and ordering guarantees
- external systems, optional dependencies, and plugin boundaries
- error conditions and validation rules
- concurrency, cancellation, and restart behavior
- production call paths and existing tests

Treat documented behavior and durable formats as contracts even when their
implementation is small.


## Coverage Model

Evaluate coverage across several dimensions. A package is not adequately
tested merely because every line executes once.

### API Coverage

Every supported public operation should have at least one direct test. Include
constructors and class methods when they perform validation or normalization.

Check that tests cover:

- accepted inputs and returned values
- defaults and omitted optional values
- invalid input and stable error messages
- calls through the canonical public entry point
- protocol implementations and plugin factories

### Decision Coverage

Identify meaningful branches rather than mechanically testing each `if`.
Cover decisions that change observable behavior, including:

- empty, singular, and multiple values
- configured and default behavior
- matching and non-matching selectors
- success, failure, and cancellation
- synchronous and streaming paths
- cached and uncached paths
- local and external implementations

Use branch coverage as a discovery aid, then decide whether each uncovered
branch represents supported behavior, defensive code, or dead code.

### State Coverage

For mutable or durable components, test transitions rather than isolated
methods. Define the allowed states and exercise:

- initial state
- every valid transition
- rejected transitions
- repeated or idempotent operations
- recovery after failure
- reload or restart from persisted state
- behavior observed by concurrent readers or writers

For event-driven packages, assert the complete event sequence and important
payload fields, not only the final result.

### Boundary Coverage

Test both sides of every package boundary:

- input conversion into package-owned types
- output consumed by direct callers
- protocol conformance at plugin loading
- serialization and deserialization round trips
- database and filesystem ownership
- CLI, HTTP, scheduler, or executor integration when applicable

Mock external services at their network or process boundary. Prefer real
package objects and small fake protocol implementations over mocking internal
functions.

### Concurrency Coverage

Packages that use threads, coroutines, global state, files, or databases need
explicit concurrency tests. Include relevant cases such as:

- two independent operations running at once
- multiple operations targeting the same resource
- cancellation during blocking work
- completion racing with cancellation or shutdown
- process-safe ID allocation and durable writes
- state snapshots remaining stable while newer versions are published

Use synchronization primitives in tests to control ordering. Avoid timing-only
assertions and arbitrary sleeps.


## Test Levels

Use the smallest level that can prove the behavior.

### Unit Tests

Unit tests cover package-owned pure logic, validation, parsing, mapping,
normalization, and state transitions. They should be deterministic and fast.

### Contract Tests

Contract tests apply the same behavior suite to every implementation of a
protocol, store, adapter, or plugin family. They verify that built-in and fake
implementations satisfy the package boundary consistently.

### Integration Tests

Integration tests combine the package with its direct collaborators. Use them
for persistence, plugin loading, filesystem layouts, event projection, and
other behavior that cannot be represented accurately by a unit test.

### End-To-End Tests

End-to-end tests should cover only critical user workflows whose correctness
depends on several packages. Do not use them as the primary coverage for logic
owned by the package under review.


## Existing Coverage Audit

Create a behavior matrix before adding tests. For each public operation or
important invariant, record:

```text
behavior | current test | level | gaps | priority
```

Then inspect existing tests for assertion quality. A test that only checks that
execution succeeds does not cover the returned data, emitted events, durable
state, or side effects.

Establish a reproducible baseline before changing tests. Record the focused
test selection, passing test count, line coverage, branch coverage, and missing
lines for each package module. Run the same selection after each test pass so
that changes can be attributed to the new tests rather than a different test
set.

Useful repository checks include:

```text
find src/toolang/<package> -type f
rg '^(class|def|async def) ' src/toolang/<package>
rg 'toolang\.<package>' tests
rg '^def test_|^async def test_' tests
uv run pytest -q <focused-test-files>
```

When coverage tooling is available, collect branch data for the focused tests:

```text
uv run --with coverage coverage run --branch -m pytest <focused-test-files>
uv run --with coverage coverage report --show-missing \
  --include='src/toolang/<package>/*'
```

Coverage output identifies code to inspect; it does not decide which tests are
required. Compare the final report with the baseline, but do not use a single
percentage as the acceptance threshold. Exclude generated code only when its
generation and consumption are tested elsewhere.


## Adding Tests

Add tests in this order:

1. Reproduce confirmed defects with a failing regression test.
2. Cover public behavior that can corrupt durable state or produce incorrect
   external requests.
3. Cover concurrency, cancellation, and ordering guarantees.
4. Cover validation and malformed boundary inputs.
5. Cover untested decisions in frequently used code.
6. Remove dead branches instead of adding tests solely to execute them.

Keep each test focused on one behavior. Name the test after the condition and
expected result. Assertions should include all contract-relevant output, while
avoiding incidental implementation details.

Use fixtures for meaningful reusable environments such as an agent layout,
store, plugin set, or trace collector. Do not hide the behavior under test
behind a large fixture or helper stack.


## Test Organization

Organize tests by owned concept and behavior, not necessarily one test file per
source module. Split a test file when its scenarios no longer form one readable
concept family.

Package tests should remain independent of test execution order. Each test
owns its files, database, environment changes, global registrations, and
background tasks. Restore or isolate process-global state explicitly.

Test-only implementations and sample plugins belong under `tests/fixtures` or
the relevant test module unless they are intentional, documented examples in
the distributed package.


## Completion Criteria

A package test pass is complete when:

- every supported public behavior appears in the behavior matrix
- critical branches and all state transitions are covered
- malformed boundary inputs have explicit expectations
- serialization and persistence round trips are verified
- concurrency guarantees are tested without timing assumptions
- built-in protocol implementations pass shared contract tests
- confirmed defects have regression tests
- uncovered code is classified as unsupported, defensive, external-facing, or
  dead
- focused tests and the repository's required checks pass

The final report should state focused tests run, important uncovered risks, and
any code recommended for removal. Report behavioral gaps separately from raw
line and branch percentages.
