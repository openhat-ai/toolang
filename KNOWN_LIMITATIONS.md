# Known Limitations

These limitations apply to Toolang 0.3.0. Toolang is currently alpha software,
and compatibility boundaries may change between minor releases.


## Platform Support

- Toolang currently targets Linux and macOS. Windows is not supported because
  runtime locking, process management, sandbox startup, and the shell tool use
  POSIX facilities such as `fcntl`, process groups, signals, and `/bin/sh`.
- Docker hosting requires a working local Docker installation. The `none`
  sandbox runs directly on the host and does not provide operating-system
  isolation.


## Models And Live Integrations

- An API key for a configured provider or a running Ollama service is required
  before an agent can make model calls.
- Provider behavior and supported multimodal features vary by model. Real-model
  and live terminal tests are opt-in and are not part of the default test run.
- Agent-home setup overrides are not supported yet. Model and tool setup is
  resolved from root-scoped configuration and the process environment.


## Security And Trust

- Remote agents and caps are executable instructions, not passive documents.
  Run only sources you trust, especially when they enable shell, filesystem,
  network, or service tools.
- The shell tool constrains its working directory to the agent home, but the
  command itself runs with the host process permissions and environment. Use an
  isolated sandbox for untrusted workloads.
- The local agent HTTP API does not currently authenticate requests. Keep it
  bound to a loopback address and do not expose it directly to an untrusted
  network.


## Surface Coverage

- The terminal chat supports quick commands, persistent settings, and runnable
  overrides. The WebUI does not yet implement the same complete submission
  profile.
- Direct terminal chat currently supports only the `none` sandbox. Running the
  interactive TUI itself inside another hosting environment is not supported.
- Roaming and visiting agents support only the command subsets documented in
  `docs/api.md`; not every resident-agent management command is available.


## Upgrade Compatibility

- Programs written for 0.2.7 use the legacy `use` and `thunk` syntax and do not
  parse under the current language. They must be migrated to `with`, `agic`,
  and, where applicable, `flow` declarations.
- Version 0.2.7 `runs.db` files use schema 9. The current runtime does not
  migrate that schema, so old execution history cannot be opened in place.
  Back up the Toolang root before upgrading and preserve or move the old
  `.runtime/runs.db` files before allowing the new runtime to create its
  stores.
- External plugins built against legacy internal modules may require import and
  protocol updates. Stable plugin-facing values and protocols now live under
  `toolang.base`.
