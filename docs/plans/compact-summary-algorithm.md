# Text-only compaction algorithms

Status: revised design approved in conversation; no compatibility required.

## Contract and ownership

- `base/types/compaction.py` defines `CompactionResult(thread, begin, end,
  summary)`. Every field is required nonempty text; coverage is `[begin, end)`.
  Execution parses references and validates coverage against visible history.
- Bundled and external `agic compact(thread: Text, begin: Text, end: Text,
  previous_summary: Text) -> Text` only read history and return a summary.
  An absent previous summary is `""`. The bundled prompt uses implicit user
  prose, read-only history tools, `recall = none`, and `context: none`.
- Model-call preflight owns automatic admission and boundary selection. The
  compact tool implementation lives beside other execution tools. CLI owns
  argument resolution and external source loading; its usage stays unchanged.
- Execution shares algorithm invocation, cancellation, result assembly and
  publication. Callers adopt results. Algorithms never update runtime state.

## Persistence

Store complete results directly as immutable `compaction` records in the
existing compact Thread control log. A horizon selects the record's
`payload/result`; assembly selects its summary. This reuses durable reference
resolution without introducing a new record namespace or synthetic Run.
The algorithm Run keeps its original Text output. FORGET publishes a result
without any Run or model call; its CLI response omits `run`.

Publication validates current coverage and terminal roots inside its write
transaction. Keep the compaction permit across generation and publication;
queued CLI requests reject changed roots or summary generations. Empty, failed,
or canceled generation never publishes a result. Each published result is
self-contained; remove legacy null normalization and producer-chain decoding.

## Acceptance and risks

Update execution types/records/store, history assembly, preflight integration,
compact tool, CLI, and affected documentation/tests. Verify DEFAULT, external
algorithms, FORGET, incremental coverage, invalid output, cancellation,
concurrency, restart/replay, and post-compaction reasoning/output budgets.
Run default offline checks and opt-in real-provider tests.

Old Run-output horizons and old algorithm signatures are unsupported. Summary
quality remains model-dependent. Full results remain immutable even when
rewinding history makes their coverage ineligible.
