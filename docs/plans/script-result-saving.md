# Script Result Saving

Status: Implemented

## Goal

Stop writing a successful Script Run result to stdout by default. Persist every
Run result in the execution store as today, and write it during invocation only
when the caller explicitly selects a destination.

Success means:

- a normal Script invocation reserves stdout and emits no result there;
- `--save -` writes the durable root Run result to stdout;
- `--save PATH` writes the same result to a file;
- progress and terminal Run status remain on stderr; and
- a later result command can export the stored value with the same destination
  and serialization contract.

## User Contract

Every generated Script runnable command gains one option:

```text
--save DEST  Save the Run result to PATH, or use - for stdout.
```

Canonical behavior:

```text
too workflow.too research "topic"                  # stdout is empty
too workflow.too research --save - "topic"         # result on stdout
too workflow.too research --save result.json "topic"
```

`--save` is the selected name. `result` is the durable resource; `save` is the
explicit write action. `--output` is avoided because Toolang already uses
`output` for internal Step and Run fields, while `--export` could imply a full
portable Run archive rather than only its result.

The corresponding future retrieval command is:

```text
too result RUN_ID
too result RUN_ID --save PATH
too result RUN_ID --save -
```

An explicit `too result RUN_ID` may write the result to stdout by default because
retrieval is the command's sole purpose. Its `--save` option uses exactly the
same file and serialization behavior as Script invocation. `inspect` remains a
human/JSON view of Run structure and diagnostics; it does not become the raw
result export surface.

## Save Semantics

- No `--save`: do not load or render the result for stdout. The result remains
  queryable through `RunStore.run_output`.
- `--save -`: write only the serialized result to stdout. Progress remains on
  stderr.
- `--save PATH`: write the serialized result to that file and leave stdout
  empty.
- `--quiet` suppresses progress but does not suppress an explicit `--save`.
- A failed or canceled Run does not write a result or change the destination.
- A save failure reports an error on stderr and returns a nonzero command status;
  it does not change the already-persisted successful Run.
- Parent directories must already exist. A directory or non-regular destination
  is rejected.
- File output is written to a sibling temporary file and atomically replaces an
  existing regular destination. This makes repeated automation deterministic
  without exposing partial content.
- An empty successful result writes zero bytes when a destination was selected.

## Serialization

Serialization is shared by Script saving and the future result command:

- all-Text results use the exact concatenated text encoded as UTF-8;
- mixed or structured Part results use compact UTF-8 JSON from
  `parts_to_data`, with no display indentation; and
- saving adds no implicit trailing newline.

The database remains authoritative. Saving never changes the stored result and
does not infer a format from the destination extension.

## Scope

In scope:

- add `--save DEST` to generated Script runnable commands;
- make default successful Script stdout empty;
- share one result serializer between stdout and file destinations;
- atomic file replacement and save-error handling;
- update Script help, tests, and presentation documentation; and
- reserve the `result` command vocabulary and shared save contract for later
  durable export work.

Out of scope:

- implementing `too result` in this change;
- changing execution records, Run output persistence, or pointer resolution;
- adding result format flags or extension-based conversion;
- exporting complete Run history, events, metrics, or logs; and
- changing Chat result display.

## Implementation Touchpoints

- `src/toolang/cli/toolang/commands/script.py`: generated option, destination
  validation, default stdout behavior, and result saving.
- `src/toolang/cli/common/`: a narrow shared result serialization/save helper
  suitable for the future `result` command.
- `tests/unit/cli/test_script_command.py`: option parsing, destination behavior,
  failures, quiet mode, and exact bytes.
- `tests/integration/cli/test_script_local.py`: persisted result with empty
  default stdout plus stdout/file saving.
- Script help and execution presentation documentation: stdout is opt-in via
  `--save -`.

## Acceptance Tests

1. A successful text Run without `--save` emits no stdout, retains its result in
   the database, and keeps progress/footer on stderr.
2. `--save -` emits the exact text result with no added newline.
3. `--save PATH` atomically writes the exact text result and emits no stdout.
4. Structured Parts use the same compact JSON bytes for `-` and a file.
5. An empty successful result creates an empty selected file.
6. Failed and canceled Runs leave an existing destination unchanged.
7. A missing parent, directory destination, or write failure returns nonzero,
   reports one clear error, and leaves no temporary file.
8. `--quiet --save -` emits only the result; `--quiet --save PATH` emits neither
   stdout nor progress.
9. Runnable help documents `--save DEST` and the `-` convention.
10. The default offline verification suite passes.

## Risks

- Existing shell pipelines may rely on implicit stdout. This is an intentional
  breaking behavior change and must be called out in release notes; `--save -`
  is the migration path.
- Atomic replacement can overwrite an existing regular file. The explicit
  destination is treated as overwrite authorization; directories and special
  files remain protected.
- Text without a trailing newline can leave an interactive shell prompt on the
  same line. Exact saving takes precedence over terminal decoration because
  stdout is now an explicit data channel.
