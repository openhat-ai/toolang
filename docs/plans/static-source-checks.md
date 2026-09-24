# Static Source Checks

Approved for implementation in the conversation. Expose source-determined errors
through the shared language validator and `too parse --check` before execution.

- Validate template syntax, reserved roots, declared parameter references, and
  type references. Reuse pure template rules with runtime rendering.
- Walk each flow in statement order using declared signatures. Track local
  availability, value type, and item/list shape; check required arguments,
  collection inputs, and settle's implicit-seed output contract.
- Track the nearest repeat/settle window. Initializers use the outer scope;
  until uses post-body locals. Missing historic frames remain normal warm-up.
  Unknown inherited scopes, dynamic values, and conversions remain runtime checks.
- Analyze loops conservatively, including zero iterations and changing locals.
  Do not infer signatures through callees or prepare execution resources.
- `parse --check PATH...` accepts files/directories or sole stdin, recursively
  discovers and deduplicates `.too` files, emits no AST, reports the first error
  per file, continues across files, and exits 1 on errors. Reject tree/output
  options except explicit `--ast`. No arguments retain help behavior.
- Diagnostics carry structured one-based positions, anchored to the relevant
  declaration/statement when a token position is unavailable. Preserve AST data.
- Keep CST inspection, formatting, highlighting, template lookup, and runtime
  safety checks unchanged. No grammar changes or external resource resolution.

Touchpoints: `common/template.py`, `lang/{errors,types,validate,lower,contracts}.py`,
new flow validation, CLI source commands, source-command documentation and tests.
Invalidate prepared source caches so older validated ASTs are rebuilt.
Share only pure rules; the language layer must not import execution modules.

Acceptance: reproduce missed errors; cover forward/recursive types, legal
template sections, optional arguments, array items versus flow lists, bindings,
zero-count and nested loops, warm-up, inherited context, stdin, path discovery,
read failures, stable diagnostics, and unchanged output modes. Run repository
lint, format, type, full offline tests, and tracked examples. Primary risk is
false positives where runtime context or conversion affects validity.

Open decisions: none.
