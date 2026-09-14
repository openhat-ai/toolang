# Known Limitations

These limitations apply to Toolang 0.3.0. Toolang is alpha software, and
compatibility boundaries may change between minor releases.


## Platform And Execution

- Toolang targets Linux and macOS with Python 3.11 or later. Windows is not
  supported: locking, process management, sandbox startup, and shell tools use
  POSIX facilities such as `fcntl`, process groups, signals, and `/bin/sh`.
- The `host` sandbox runs with the host process's permissions and provides no
  operating-system isolation. Docker execution requires a working Docker
  installation. The interactive TUI remains local while its workload can run
  in a sandbox or an attached compatible agent server.
- Toolang does not automatically resume an unfinished run after its owner
  process exits. Durable history and explicit retry/rerun remain available
  where the recorded run state permits them.


## Models And Clients

- Model calls require credentials for a configured provider or a running local
  model service. Model capabilities and multimodal support vary by provider.
- Live-provider, live-terminal, and Docker tests are opt-in; passing the default
  offline suite does not verify a user's credentials or hosting environment.
- Agent-management commands have different target support; see
  [CLI/API documentation](./docs/api.md).
- Run-event SSE has no historical replay cursor. Clients reconstruct state from
  durable records; a disconnected stream does not imply that its run stopped.


## Security And Trust

- Remote agents and caps can enable executable tools. Review sources before
  granting shell, filesystem, network, or service access.
- Workspace paths choose a filesystem or shell working location; they do not
  restrict a shell process's operating-system permissions. Use an appropriate
  sandbox for workloads that require isolation.
- The agent HTTP API does not authenticate requests. Keep it bound to a
  loopback address unless a trusted external access boundary protects it.


## Compatibility

- Execution stores must use schema 43. Incompatible stores are rejected without
  an in-place migration, including stores created by internal development
  snapshots. Preserve a complete Toolang root backup and the matching runtime
  if that history is needed.
- Language, configuration, storage, and plugin contracts may change while
  Toolang is in alpha. Build plugins against the interfaces in `toolang.base`;
  internal implementation modules are not stable extension points.
