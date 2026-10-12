# Independent work thread naming

Status: approved by the request to implement this rename and create a PR.

## Goal and scope

Name thread purposes consistently: `chat`, `talk`, `chore`, `work`, and `script`.
Replace `worker` with `work` for independent work; every execution retains a
`run_` ID. This is a naming change, not a new driver or execution mode.

## Decisions

- Flow and agic spawn issue `work_` thread IDs, persist `origin="work"`, and
  publish that origin in thread events, regardless of the source thread kind.
- Rename `ThreadPrefix.WORKER` to `ThreadPrefix.WORK` and the profile metric
  `threads.worker` to `threads.work`.
- Preserve spawn ownership, ancestry, empty history, admission atomicity, and
  cancellation behavior. Update active documentation and model guidance.
- Provide no old-prefix aliases, compatibility conversion, or data migration.
  Integrations must use the new prefix, origin, enum member, and metric key.
  Historical record IDs are not rewritten.

## Touchpoints and verification

Implementation: `execution/{types,store}.py`, `execution/executor/spawn.py`,
`api/routers/agent.py`, and spawn tool/protocol instructions. Documentation:
`docs/{ids,chat,work,tools,api}.md` and `CHANGELOG.md`.

| Scenario | Pass condition |
| --- | --- |
| Spawn from each driver or script | Flow and agic admission return `work_` threads and `run_` runs; stored metadata/events use work and retain ancestry |
| Creation | Work contexts use work; worker is no longer an issuable prefix |
| Observation | Profile counts work threads under work without a worker field |
| Lifecycle | Existing spawn atomicity, ownership, result, and cancellation checks pass |
| Guidance | Active examples and prompt descriptions use work; no unrelated worker concepts are renamed |

The compatibility risk is consumers that interpret the previous prefix or metric
key. Verify the new contract with the existing driver, spawn, thread-control, and
profile suites, then run all default checks. No open questions remain.
