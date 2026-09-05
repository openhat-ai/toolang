# Remove execution ejection markers

## Goal and approved scope

Remove obsolete Run/Step ejection metadata now that Thread controls project
fork/rewind history and retry physically deletes its invalid execution suffix.
Keep logical history and physical ownership distinct. Also rename Control
payload `locals` to `input`, without changing payload semantics or ModelCall
assembly.

## Decisions

- Remove `ejected_by` from Run/Step records, tables, codecs, and Step projections;
  remove `RunInfo.ejected` and its projection helpers.
- Rename `locals` to `input` in Run, Rerun, Retry, Execute, Steer, and Cancel
  payloads, codecs, and field references. Keep `payload`, Local values, and
  execution-time locals unchanged; add no compatibility alias.
- Physical record, subtree, and unfiltered collection reads use existing rows.
  Thread-selected history continues to use the shared Thread projection.
- Remove physical-read `include_ejected` options. Rename the logical-history
  option to `include_rewound`; it ignores only the selected Thread's rewinds,
  without changing its captured fork prefix.
- Keep controls of deleted child Runs for audit. Existing collection and Pointer
  lookup contracts still exclude controls whose owning Run no longer exists.
- Bump schema 35 to 36. Reject incompatible databases unchanged; do not migrate.

## Touchpoints and acceptance

Update execution records, store, history, schemas, and their readers/tests.
Verify canonical fields and payload round trips, CLI rejection of removed fields,
input field references, unchanged fork/rewind
physical records, logical visibility, child/subtree inspection, retry deletion,
orphan control filtering, and restart reconstruction. Run the default offline
verification suite.

The main risk is confusing logical rewind with physical deletion. No open
questions remain for this PR; payload simplification and control consumption
positions are separate follow-ups.
