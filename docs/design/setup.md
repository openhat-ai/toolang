# Setup

`AgentSetup` is one immutable generation of installed runtime resources and
execution policy. [State](state.md) prepares authored language, caps, and
workspace declarations. Setup connects execution to installed plugins, model
catalogs, credentials, and process-environment facts. Neither publication
starts or resumes a Run.

## Preparation boundary

```text
placement-specific config + environment + entry points + catalog snapshots
  -> SetupWatcher candidate
  -> immutable AgentSetup generation
  -> independently materialized resource views
  -> captured RunSpec
```

Call sites supply concrete layout, sandbox, source paths, and policy overrides.
The watcher reads origin-aware configuration and captures catalog snapshots;
plugins receive their own resolved factory configuration. Core configuration
does not dereference a plugin's secret-reference fields. [Plugins](plugins.md)
owns those contracts and [Models](models.md) owns catalogs and connection routes.

Resident and visiting layouts read the
[root/home authored scopes](layout.md#root-place-and-home). Roaming layouts use
the captured project layers described in [Scripts](scripts.md#directories-and-configuration),
with relative paths bound to the declaring file. Environment selection is
independent of these TOML layers.

## Environments

Only the selected Toolang `ROOT/config.toml` accepts environment name lists:

```toml
[env]
keep = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "TZ", "TMPDIR", "TMP", "TEMP", "TERM", "COLORTERM", "NO_COLOR"]
pick = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "XAI_API_KEY"]
```

These are the built-in defaults. Each present list replaces its default; `[]`
clears it. Names are exact and case-sensitive. Invalid names, duplicates, wrong
types, unknown fields, and `[env]` in home/project TOML are rejected. TOML accepts
names only; keep secret values in dotenv files outside Git.
Add `.env` and `.*.env` to `.gitignore` in repositories containing these files.

Without `ROOT/.env`, root commands inherit the caller's complete environment.
With it, they receive `keep` from the caller plus the file's bindings. Agent
commands start from that root result and receive `keep` plus one selected agent
file, or the first nonblank variable in `pick` when no file exists.

| Placement | Selected agent file |
| --- | --- |
| Resident | `ROOT/.alice.env`, then `HOME/.env` |
| Roaming | `ROOT/.{encode_flatname(canonical_source_path)}.env`, then `SOURCE_DIR/.toolang.env` |
| Visiting | `ROOT/.{encode_flatname(canonical_requested_ref_or_url)}.env` |

Nonresident root filenames use `toolang.common.flatname.encode_flatname`.
Roaming identity is the canonical absolute `.too` path, resolving symlinks;
visiting identity is the requested source standardized by the source parser,
not the fetched content or resolved commit. GitHub URL/ref aliases share a
canonical ref, while a shorthand such as `gh:owner/alice` remains a shorthand.
Other HTTP URLs retain their requested spelling, including query and escapes.
For example, `/work/aid.too` uses `ROOT/.%work%aid.too.env`, and
`gh:owner/alice` uses `ROOT/.gh+3aowner%alice.env`.

Encoding preserves case and produces the full filename without truncation or
hash fallback. A filename exceeding the filesystem's component limit is treated
as absent, allowing the local or automatic policy to work; other inspection/read
errors fail resolution. Case-distinct identities may share a file on a
case-insensitive filesystem. Nonresident agents never use same-name resident
files, visiting home dotenv, project `.env`, or ancestor `.toolang.env` files.

The first existing file replaces lower-priority agent files completely. Its
bindings are applied after `keep`; explicit assignments can override kept names.
Even an empty agent file disables `pick` and lower-priority files.

```dotenv
# Bare names inherit from the supplied parent, if present.
ANTHROPIC_API_KEY
# Assignments are literal; an empty assignment preserves an empty string.
HTTP_PROXY=http://localhost:8080
OPTION=
LITERAL='${HOME}/literal'
```

Ordinary comments and quoting are supported without interpolation. Malformed,
duplicate, or unreadable bindings fail with path/line diagnostics that omit
values. A bare agent name cannot recover a caller variable excluded by the root
file. Centralize values in `ROOT/.env` and normally use bare names in agent
files. Root agent files are independent of generated homes and runtime instances,
so policy survives home removal and is reused for the same stable identity.

The CLI installs the selected environment before user plugin loading; adapters,
tools, child processes, inspection, and `cd` share it. Controller startup inputs
such as ports, catalog paths, and policy overrides are captured separately and
passed as concrete settings. Finite generated runtime facts take precedence over
authored values. Selection logs report source paths and the picked name, never
values. HTTP source filenames are redacted in logs and errors because their
reversible spelling may contain URL credentials; visiting identities are omitted
from selection logs. [Hosting](hosting.md) owns guest transport.

Setup captures its environment when created and retains it across refreshes.
Changing dotenv or shell values requires a restart; attaching to a running server
keeps that server's environment. This replaces the earlier full process
inheritance, process-over-dotenv precedence, and dotenv hot reload: explicitly
list required credentials, proxy/certificate variables, or other dependencies in
an agent file, and restart existing servers after migration.

This controls initial propagation, without adding filesystem, memory, or OS
isolation. User code and shell startup files can load other values; admitted
credentials are available to the entire agent, including its tools.

## Policy

Policy resolves field by field from built-in defaults through root/home or
project configuration, process-derived overrides, and explicit CLI overrides.
`setup.config` parses source tables; `setup.models` validates configured model
choices when the lazy catalog loads. The watcher coordinates publication;
execution supplies handler/thread baselines and applies the shared typed model
operations. Request/session policy applies when preparing a Run. Model/tool
allow policy belongs to Setup; cap allow policy belongs to State. External
resource ceilings can only narrow the published bases. An empty allow set denies that resource;
a zero limit is active, while `none` disables a limit.

The configuration groups are `allow`, `model`, `limit`, `talk`, and `compact`.
Allow queries select common resources; model and limit settings supply ordinary
root defaults. Talk overlays model/limit settings for automatic message handling,
and compact settings apply to all thread kinds. Limits use the scopes defined by
[Execution](execution.md#run-execution). There is no runtime
default-runnable setting; [Drivers](drivers.md) resolve their handlers. Chat's
runnable default is session state.

[Queries](queries.md) owns matching. [Models](models.md#model-selection) owns model
expressions and compaction configuration. Startup CLI/environment overrides are
fixed watcher inputs; later configuration refreshes do not rewrite accepted Runs.

## Lazy views

Models/providers, adapters, catalogs, toolset plugins, and leaf tools are
separate generation-local views. Accessors materialize their dependencies and
memoize successful values with thread-safe single-flight loading. Listing
toolset plugins need not construct leaf tools, and inspecting tools need not
discover models.

A failed load publishes no partial value and permits another attempt. Lazy
memoization does not change captured inputs. All/effective views share the same
underlying resources; allow filtering does not mutate the complete snapshot.
[Queries](queries.md) owns their inspection projections.

## Publication and lifetime

Refreshes serialize candidate checks. A changed valid candidate replaces the
current Setup; an unchanged candidate reuses it. Rejection records diagnostics
and preserves the last valid Setup. Initial preparation fails when no prior
valid generation exists. Lazy resource failures remain visible to the accessor
that requested them.

The watcher retains only the current generation. Accepted Runs hold their own
references, keeping previous generations alive as needed; descendants inherit
the accepted Setup. Later State publication can update model-call cap/runnable
resolution, while installed models, tools, environment, and policy remain with
the captured Setup. [Execution](execution.md#state-capture) owns these boundaries.

Setup revisions are provenance, not a durable resource replay store. Views stay
in memory; no `.setup/` model cache or historical Setup table is maintained.
Model Steps retain their actual normalized calls for inspection.
