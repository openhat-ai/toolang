# Inline Skills

## Status and Goal

Approved for implementation by the user on 2026-10-11: collect and apply
text-only skills declared in `.too` modules, using the existing skill discovery
and lazy guidance protocol.

Success means a valid inline skill becomes a module-local State capability,
appears in permitted model triggers, and supplies its captured body when picked.

## Verified Baseline

At `0a432978`, `Program.from_source()` accepts `skill name:` with a nonempty
`description` property and body. Existing language tests cover that contract.
However, `state/state.py:EMBEDDED_CAP_KINDS` excludes `skill`, so State preparation
silently discards the declaration. An offline reproduction parsed
`skill code_review:` but produced an empty `state.module_caps["agent"]`.

The existing runtime already emits skill descriptions and loads selected bodies
through `_toolang__pick`; the missing behavior is State materialization.

## Authoring Contract

Keep the existing grammar:

```too
skill code_review:
  description = Use when reviewing code changes for correctness and regressions.

  Read the diff and relevant implementation.
  Identify concrete bugs and missing regression tests.
  Report findings with file locations and reasoning.

agic review:
  skills = skill/code_review

  Use the code_review skill to review the current changes.
```

Both description and body remain required. `description` remains the only
supported skill property. The body is literal guidance, with no parameter
binding, template expansion, or executable flow semantics. Selecting a skill
makes it available; selection does not eagerly load or execute it.

Support script modules, `agent.too`, and `flows/<name>.too` through their shared
State preparation path. Follow [program module visibility](../program.md) and
[capability selection and precedence](../caps.md): inline skills have `here`
scope, cannot leak between modules, and cannot bypass resource ceilings.
Preserve existing duplicate and same-scope conflict errors.

## State and Runtime Design

- Collect every validated cap declaration directly, without a second kind
  whitelist that can silently discard skills. Reuse the existing single-file
  materialization: `shape="file"`, `form="inline"`, `origin="local"`, and
  `inline://skills/<name>`. Store frontmatter plus body at
  `files/caps/inline/<module>/skill/<name>.md` in the immutable home layer.
- Preserve the source module path, declaration line, and module digest. Public
  model identity remains `skill/<name>`; inspection points to the authored
  `.too` file and line. Inline definitions are edited through that source file.
- Reuse trigger generation and `_toolang__pick(kind="skill", ref="skill/<name>")`.
  Initial model input contains the description; a successful pick publishes the
  captured body as skill guidance. Existing revision and recall rules apply.
- Share one completeness check between current-State loading and cache reuse:
  every declared `(kind, name)` must exist in its module's unfiltered inline cap
  set. Reject incomplete current loads so watchers rebuild on startup even when
  authored bytes are unchanged. Keep the existing layer schema; explicit
  revision loads preserve historical snapshots without applying this check.

Directory-backed skills and `with skill ...` references retain their existing
behavior. This phase does not package scripts or assets, define relative asset
paths, add skill parameters or tool permissions, or export `SKILL.md` bundles.
Those features require a separate definition if requested.

## Implementation Touchpoints

| File | Change |
| --- | --- |
| `src/toolang/state/state.py` | Include skills in embedded collection and reuse materialization. |
| `src/toolang/state/prepare.py` | Reject incomplete current-State loads and cache reuse with one shared check. |
| `tests/unit/state/test_prepare.py` | Cover materialization, scope, precedence, snapshots, and cache upgrade. |
| `tests/integration/execution/test_pick_guidance.py` | Exercise inline trigger, pick, and denied-resource behavior with a scripted model. |
| `docs/program.md`, `docs/caps.md` | Explain inline skill syntax, loading, and text-only boundaries. |
| `CHANGELOG.md` | Record the behavior change through the repository changelog runnable. |

No grammar or protocol change is expected. Existing tests in
`tests/unit/lang/test_program.py` remain the syntax contract.

## Acceptance Checks

1. A valid inline skill in each supported module form becomes a readable,
   file-shaped capability with its description, body, source path, and line.
2. Initial model input exposes its trigger without its body; an exact permitted
   pick delivers its body and revision through the existing recall protocol.
3. Resource exclusion hides the trigger and denies a pick; `skills = skill/name`
   selects the inline skill within the effective resource ceiling.
4. An inline skill wins over a same-name home/root skill in its owning module;
   sibling modules do not inherit it. Inline/reference conflicts in one module
   still fail preparation.
5. Source edits produce a new capability revision and body; old snapshots retain
   the old body. A removal withdraws the capability through existing State rules.
6. A baseline cached layer that omitted an inline skill is rejected as current
   State and rebuilt by watcher startup without source edits or manual cache
   deletion. The repaired layer is reused on the next preparation; explicit
   historical loads and layers without inline skills remain usable.
7. Existing syntax rejection, directory skills, remote references, inspection,
   and replay tests remain green. All new runtime tests stay offline.

Before an implementation commit, run the default verification from
[AGENTS.md](../../AGENTS.md). For this definition, validate the example against
the parser, verify source touchpoints and links, and run `git diff --check`.

## Risks and Approval

Previously ignored declarations will become active and may shadow same-name
home/root skills or expose same-module conflicts. Authors who want the previous
effective skill must remove or rename the inline declaration. File-shaped inline
skills intentionally have no bundled asset list. Cache upgrade coverage is
required to avoid a change that works only in fresh homes.

There are no open design questions within the approved text-only scope.
