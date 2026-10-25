# Preserve agent source configuration in Docker

Status: implemented locally; approved in discussion on 2026-10-25.
Patch and internal binding shapes describe the approved implementation contract.

## Goal and boundary

Host and guest must prepare State from the same configuration inputs. Today,
Docker mounts a generated replacement `config.toml`; its different contents
cause repeated root revisions when host commands and the guest share `.state`.
Move runtime corrections into the existing environment channel and host argv
construction. Keep `current` watching, revision formats, and guest directory layout.

## Configuration ownership

Let `R` be the host Toolang root, `P` the agent PLACE, `H=P/agents/NAME`, and
`G` the guest agent root, normally `/root/.toolang`.

| Process / placement | Configuration inputs |
| --- | --- |
| Resident agent | `P/config.toml` + `H/config.toml`; here `P=R`. |
| Visiting agent | Its managed PLACE + HOME; keep PLACE configuration empty by default. |
| Roaming agent | Discovered `toolang.toml` layers captured into `P/config.toml` + agent-local HOME. |
| Hub server | `[hub]` in `R/config.toml`; agents use `[teaming]` in their own layers. |

Nonresident agents do not implicitly inherit `R/config.toml` as agent
configuration. Host launch environment policy and registry ownership still use
`R`; that is separate from agent configuration. Splitting `hub.toml` is out of scope.

Roaming example, after host preparation:

```text
/Users/bryan/projects/demo/
  aid.too
  toolang.toml                    # Authored project configuration
  toolang.catalog.json           # Optional project catalog
  .toolang/<root-code>/<place-code>/
    config.toml                  # Captured project ROOT layer
    catalog.json                 # Link to selected project catalog, if any
    agents/aid/config.toml       # Agent-local HOME configuration only
```

Keep project discovery/merge order and source-relative origins. Move only
Toolang-owned projections/links out of HOME; preserve authored HOME files.
If relocation conflicts with authored content, report it without overwriting.
The project capture is shared by host and guest, not regenerated for Docker.
Setup retains ordered project sources tagged as root scope, followed by HOME;
root-only resolvers consume their effective root view. Preserve per-file origins.

Keep roaming's flat project `[workspaces]` syntax. Feed its grants through the
existing per-agent root-policy receipt instead of moving the whole project
into HOME. Preserve HOME fallback/root override precedence. State scanners,
workspace receipts, and watchers must use `P/config.toml`, not global `R`.

## Configuration corrections: environment → in-memory patch

Mount original `P/config.toml` read-only at `G/config.toml`. Keep HOME's original
configuration through its existing mount. Neither configuration receives a
Docker-specific replacement, including for relative-path rebasing.

For example, HOME configuration has identical bytes on both sides:

```toml
[workspaces]
repo = "/Users/bryan/projects/demo"
```

The host stages resolved environment bindings and corrections together:

```text
host: active/guest.env → guest: G/.merkle.env
```

```dotenv
TOOLANG_CONFIG_PATCH='{
  "origins": {
    "root": "/Users/bryan/.toolang/config.toml",
    "home": "/Users/bryan/.toolang/agents/merkle/config.toml"
  },
  "defaults": {"root": {"teaming": {"human": "bryan"}}},
  "home": {"workspaces": {"repo": "/root/.toolang/workspaces/merkle/repo"}}
}'
PROVIDER_API_KEY=<redacted>
```

`.merkle.env` illustrates the existing policy filename; retain source-based
filenames for roaming/visiting. Continue ordinary environment selection and
parsing before plugin construction. Files remain mode `0600`, mounted read-only;
values can enter process environments and must not enter logs, Git, or State.

Patch and source rules:

- `defaults` fills missing leaves in the named scopes; explicit source values
  win. Top-level `root`/`home` overlay the execution view. Recursively merge
  objects, replace scalar/array leaves, and preserve omitted fields. Reject
  unknown envelope keys, non-object scopes, and nulls; no deletion/expression DSL.
- `origins` contains absolute host source filenames, not guest files
  to open. Additional absolute reference keys retain canonical symlink/tilde
  targets captured on the host. Preserve this provenance; never
  rebase against guest HOME/cwd or resolve host paths through guest symlinks.
  Named `root/catalog.json` and `home/catalog.json` origins pin automatic catalog
  selection. For unchanged workspace declarations, use newer accepted canonical
  identities from shared State without changing the captured execution bindings.
- CLI orchestration parses the envelope and passes concrete source contexts to
  parsers. Build canonical State from original bytes and host origins; only
  then derive the patched execution/Setup view. Persist neither the envelope
  nor translated values. Reject State-owned policy changes in runtime patches.
  All Setup consumers, including tools/teaming, must use the supplied scoped
  view rather than rereading unpatched files.
- Carry the host's human fallback in `defaults.root.teaming.human`; explicit
  human configuration, including later edits, wins. Keep root/home teaming
  enable gates and existing gateway-based Hub URL translation. Do not freeze
  explicit human/hub/enabled fields or alter `[hub]`.
- For workspaces, the captured bindings below supply canonical source identity
  and permitted execution targets. Apply a path correction only while the
  current grant matches its captured source. Removed/rebound/new grants must
  not acquire access through stale patch entries. Include implicit `lab`.
  Generate patch/argv from one captured mapping; reject inconsistent targets.
  Remove `TOOLANG_WORKSPACE_MOUNTS`; introduce no replacement workspace env map.

Use the same configuration loader on host and guest, with an empty patch when
absent. Generated launch controls win over dotenv/image values. Keep workload
bindings as environment values, not configuration entries. On refresh, rebuild
from fresh sources and the immutable launch patch; never patch a previous view.

## Option corrections: host builds the guest argv

`too` is an alias for `toolang`. Example public invocation:

```sh
too serve merkle --sandbox docker:python:3.13-slim \
  --workspace data=/Users/bryan/data --workdir repo://src
```

The host resolves options and captures grants before translating physical
paths. Extend the existing common `_serve --workspace-bindings` value from
strings to records with canonical `source`, execution `path`, and `temporary`.
Illustrative argv fragments (only the CLI-added `data` entry is shown):

```sh
# Host child
--workspace-bindings '{"data":{"source":"/Users/bryan/data","path":"/Users/bryan/data","temporary":true}}' --initial-workdir repo://src
# Guest child, constructed by the host
--workspace-bindings '{"data":{"source":"/Users/bryan/data","path":"/root/.toolang/workspaces/merkle/data","temporary":true}}' --initial-workdir repo://src
```

Include configured grants and `lab` with `temporary=false` for mount checks;
only temporary entries contribute `source` values to State's CLI additions.
Their `path` values never become State inputs. Host records use `path=source`.
This preserves one shape and validator for both runtimes. Reuse the existing
internal option; add no guest-only flags or options patch in dotenv. Validate
records at entry; the shared `--launch-schema` marker makes older guests reject
the new contract before State writes.
Directory-form `--workdir` follows ordinary grant normalization; workspace URIs
remain unchanged. Preserve canonical host grants in State/Run captures while
using guest paths for execution. `guest.sh` keeps bootstrap and `exec "$@"`;
it does not parse or rewrite Toolang options.

## Catalog mounts

Select with existing precedence, retaining selection provenance (automatic vs
explicit, not merely path equality), then apply these mount rules:

| Selected catalog | Guest file | Environment correction |
| --- | --- | --- |
| Packaged default | Guest package's bundled catalog | None; no mount. |
| Automatic agent-root catalog, including roaming companion | `G/catalog.json`, read-only | None. |
| Independently specified catalog: option, environment, or config path | `G/.inputs/catalog.json`, read-only | `TOOLANG_MODEL_CATALOG=G/.inputs/catalog.json` |

Automatic HOME catalogs retain their HOME location and precedence. For the
default guest root, the custom-path binding is:

```dotenv
TOOLANG_MODEL_CATALOG=/root/.toolang/.inputs/catalog.json
```

One selected custom catalog uses a fixed target instead of a hashed path.
Catalogs remain Setup inputs; introduce no shared catalog cache.
Guest discovery must read mounted files, not host paths retained in project
origin metadata. Catalog selection/mount targets are fixed for one launch;
switching files requires restart. Atomic replacement of file-bind sources can
leave config/catalog mounts on an old inode; preserve this existing limitation
and require a remount/restart when it occurs.

## Implementation touchpoints and acceptance

- [Source capture](../../src/toolang/up/source.py) and
  [configuration sources](../../src/toolang/common/config_sources.py): roaming
  layer ownership, origin contexts, and safe relocation of generated projections.
- [Mounts](../../src/toolang/up/mounts.py),
  [launch assembly](../../src/toolang/up/sandbox.py), and
  [argv construction](../../src/toolang/up/server.py): original files, patch,
  catalog targets, and option translation. Reuse the
  [environment channel](guest-environment-launch.md).
- [Setup](../../src/toolang/setup/) and
  [catalog selection](../../src/toolang/plugin/catalogs/models_dev/path.py):
  PLACE/HOME inputs and execution views. Audit global-root configuration reads
  in [State parsing](../../src/toolang/state/source.py), workspace composition,
  teaming, and tools; retain the existing `current` handling algorithm.

| Regression scenario | Pass condition |
| --- | --- |
| Resident guest plus repeated host `too caps` and stopped-agent inspection | After initial preparation, unchanged inputs add no root/home/composition revisions. |
| Visiting/roaming guest plus inspection of that PLACE | No repeated revisions; unrelated global `too caps` is not used as evidence. |
| Guest-visible source edits and `current` publication | Other readers refresh; a publisher's own publication does not retrigger preparation. |
| Roaming layers, relative/symlinked HOME paths, no source workspace grant | Equivalent host/guest State, correct project root policies, successful startup. |
| Configured/CLI workspaces, both workdir forms, `lab`, post-launch edits | Execution reaches mounts; captures retain source identities; removed/new/rebound grants stay unavailable. |
| Catalog cases, including explicitly selecting the default file | Packaged fallback needs no mount; explicit selection retains its override; no host-only path reads. |
| Default/explicit human, teaming disabled, source edits after launch | Explicit values remain effective; fallback owner is the host human; disable gates remain valid. |
| Invalid patch and environment confidentiality | Reject bad input without echoing values; transported secrets stay outside State/logs; authored bytes stay unchanged. |

Use deterministic offline regression tests; optional Docker probes use isolated
agents. Implementation must pass the repository's default verification.
Restart guests to install new mounts/environment; retain historical revisions.
