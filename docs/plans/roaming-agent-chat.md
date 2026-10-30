# Roaming agent Chat

Status: proposed for human approval. Build on [PR #990](https://github.com/openhat-ai/toolang/pull/990)
and its [shared-data definition](roaming-prepared-state-reuse.md). No implementation
is included in this definition. This supersedes the roaming Chat exclusion in
[the foreground ownership plan](roaming-script-instances.md) for the scope below.

## Goal and scope

Support terminal Chat for local `.too` agents on host and Docker, including
same-instance continuation through `fork RUN --chat` and `rewind RUN --chat`.
Success means normal Chat submissions and history with one foreground owner per
runtime instance, reusable shared agent data, and complete owned-runtime cleanup.

Preserve #990's paths: shared State/Setup inputs and lab under `DIR/.toolang`;
runtime records, Run stores, logs, and history under `DIR/.toolang/R8/P19`.
Use the existing Chat client/server, handler selection, sandbox options, and
session settings. Keep both `too file.too chat` and `too chat file.too`.
Resident/visiting behavior and roaming Script/rerun behavior remain unchanged.
Roaming `start`, `serve`, and `stop`, additional Chat clients attaching to an
owned instance, owner transfer, and background retention remain excluded.

## Selection and ownership

| Invocation | Runtime selection |
| --- | --- |
| `too file.too chat` | Allocate a fresh P19 and start a new Chat session. |
| `too file.too@ chat` | Use the latest retained instance; fail if none exists. |
| `too file.too@P19 chat` | Use that exact retained instance; fail if missing or mismatched. |
| `too file.too chat -t` | Select the latest retained instance and its latest recorded Thread; fail if absent. |
| `too file.too chat -t THREAD_OR_RUN` | Select the unique retained instance containing the record and continue its Thread. |

Existing [address validation](../design/agent-addresses.md#layout-and-addresses)
also applies to named runtime addresses. An explicit instance constrains record
lookup to that instance. Ambiguous records require an explicit instance.
Selection never silently falls back, allocates for a continuation, or changes
retained creation order. Help and invalid selectors/options create no instance,
Thread, Run, or owner. Exiting an empty Chat creates no execution Thread or Run.

After selection and validation, claim the instance through the existing foreground
invocation mechanism before preparation/startup and hold it through final cleanup.
A live, preparing, closing, or unverifiable owner is busy; Chat must not attach,
replace it, reproject its inputs, or allocate a fallback instance. Independent
instances may run concurrently and share #990's authored State and lab.
Restarting a released instance gets a new launch ID and retains its history.

Chat launches a server with the `chat` driver and foreground lifetime. An explicit
`--enable` must include `chat`; retain existing driver selection validation.
Resolve current invocation settings and workspace grants at the CLI boundary;
do not persist them into shared authored `current` or change Script driver defaults.
Continue recorded Threads using normal Chat settings and execution contracts.

Normal Chat exit, EOF, and fatal client failure close the owned runtime. Preserve
Chat's existing key/cancellation behavior while the session remains open.
Controller loss on host or Docker must terminate owned work through the existing
[foreground finalization contract](../design/hosting.md#execution-boundaries),
including the bounded closing deadline, record finalization, generation fencing,
and registration/resource release. HTTP stream recovery alone does not end ownership.

## History continuation

Include roaming `fork RUN --chat` and `rewind RUN --chat` by reusing the existing
record selection, Thread edit, and `_open_chat` path. Open the resulting Thread
in its selected runtime instance without allocating another P19. An already busy
instance rejects before the edit. Preserve existing behavior when Chat startup
fails after a committed edit: retain the edit, report the failure, and allow a
later explicit continuation. Plain fork/rewind keep their current live/offline
behavior. No new history format, replay protocol, or Thread-edit subsystem.

## Implementation touchpoints

- `cli/toolang/commands/chat/{__init__,main}.py`: accept roaming selectors,
  select/claim the instance, and connect normal Chat to its owned server.
- `cli/common/{execution,agent_server}.py`, `up/{invocation,config}.py`: reuse
  selection/acquisition/finalization; extend only where Chat needs explicit inputs.
- `cli/toolang/commands/thread.py`: remove roaming Chat rejection and preserve
  selected-instance continuation and busy-owner behavior.
- `tests/integration/cli/test_roaming_{instances,script_homes}.py`, Chat and
  argument tests, and `tests/integration/up/test_docker_sandbox_lifecycle.py`:
  replace rejection coverage and add the acceptance cases below.
- Update affected Chat/CLI/hosting/driver/address design contracts and generate
  the behavior entry through `too aid.too update_changelog` during implementation.

## Acceptance and verification

| Scenario | Pass condition |
| --- | --- |
| Both argument orders; bare, latest, and exact selectors | Correct fresh/retained instance; shared preparation reused; history remains instance-local. |
| Explicit Thread/Run, bare `-t`, older and ambiguous histories | Correct Thread/instance; missing or ambiguous targets fail without mutation or new allocation. |
| Help, malformed arguments, unavailable handler/driver, empty session | No unintended execution records; failed startup releases ownership and leaves readable diagnostics. |
| Two callers for one instance; separate instances; multiple Toolang roots | One owner wins per instance; loser leaves owner inputs/records unchanged; existing sharing/isolation contracts hold. |
| Successful Chat turn and resumed Thread | Normal saved input/output, policy/workspace settings, shared State reuse, and exact accepted Run snapshots. |
| Fork/rewind with Chat, including an older instance and startup failure | Correct edit/Thread in the same instance; busy rejection precedes edits; committed edits survive later startup failure. |
| Host and Docker exit, interruption, owner loss, startup failure | Owned work/services terminate within the existing deadline; records reopen; no leaked runtime, active registration, or replacement-launch cleanup. |
| Resident/visiting Chat and roaming Script/rerun | Existing lifetime, driver, selection, and cancellation behavior passes unchanged. |

Default acceptance uses offline deterministic providers and synchronized CLI
processes. Verify real Docker ownership/cleanup with the existing opt-in fixtures.
Run all [default checks](../../CONTRIBUTING.md#checks-and-review) before each code/test
commit; validate documentation links/examples and `git diff --check`.

## Risks and open questions

Ownership must cover failed startup and TUI exit without accidentally retaining
or terminating another launch. Shared labs intentionally retain #990's concurrent
write behavior. A Thread edit may succeed before Chat startup fails, as documented.
No open design questions remain; human approval of this definition is pending.
