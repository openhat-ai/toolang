# Align Docker container names with agent instances

Status: implemented; approved in conversation.

## Goal and approved scope

Make each container correspond to one root-scoped agent address and keep its
name across launches of that home. The current naming, labels, and ownership
contract lives in [Hosting](../design/hosting.md#runtime-recovery-and-logs);
[Agent addresses](../design/agent-addresses.md#short-codes) owns code formats.

The Docker adapter derives one name from explicit canonical ROOT and address
inputs. Generation labels and immutable container IDs fence later operations.
Conflicts or uncertain ownership never authorize adoption or cleanup. Existing
containers are not renamed; legacy references retain their original locators.
Addresses, CLI syntax, placement lifetimes, and host execution are outside scope.

## Implementation and acceptance

Touchpoints: sandbox request/reference types, `up/sandbox.py`, the Docker adapter
and CLI, and batched runtime observation.

- Resident, visiting, and roaming names preserve full names/codes and agree
  across intent, plan, launch reference, and inspection; maximum names and
  underscores satisfy Docker grammar.
- Restart reuses the name with a new generation; distinct roots/places differ.
- Missing/inconsistent identity fails before staging or container creation.
- Colliding root hashes, foreign labels, stale references, and name replacement
  cannot inspect or mutate another workload.
- Failed/cancelled creation and pending helpers preserve recoverable ownership;
  cleanup releases only the matching generation and uses immutable IDs.
- Legacy references keep working; the next new container uses the current name.

Keep tests offline with mocked Docker/process state and run default verification.
Full-root/address labels remain necessary because short hashes can collide.
