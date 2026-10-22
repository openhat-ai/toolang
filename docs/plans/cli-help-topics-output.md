# CLI Help Expected Output

Proposed output for [CLI Help Topics](cli-help-topics.md). These are design
transcripts, not output from an implemented feature. The existing formatter
and an in-memory native command tree verify layout and diagnostic behavior;
exact spacing follows terminal width. The examples assume an installed
`gus` agent; `flow:review` illustrates an entry defined by that agent.
`demo.too` is an in-memory fixture declaring `agic greet(name: Text)`.

## Output and style contract

- All headings share `cli.heading`; Topics is a directory of topic commands.
- Topic names and example commands use `cli.command.name`; keys, value forms,
  and parameters use `cli.argument.name`. Both are bold cyan in UV and bold in PLAIN.
- Syntax uses existing option/usage styles. Explanations and rules use normal
  foreground text; only optional metadata is dimmed. No per-section palette.
- Plain output retains headings, indentation, columns, and blank lines.
- Examples place each explanation above a full-width command. Authored shell
  continuations preserve copyable commands at 44 columns and wider.
- `too help`, `too help -h`, and `too help --help` are identical.
- For each topic, bare invocation, `-h`, and `--help` are identical. Implement
  topics as native subcommands so eager help stays on the selected topic.
- Successful topic output is reference content only: declarations describe
  the target options. Omit helper-command Usage, Options, and self-help rows.
  Native invocation errors may still show the helper command usage.
- Generic assignments use `<KEY>=<VALUE>`. `Keys:` defines supported keys
  and repetition; `Values:` defines shared value forms and restrictions.
  Key-specific value forms stay beside their keys, as in `default`. Keep
  notes with their subject instead of using a generic `Rules:` section.
  Literal keys stay lowercase; model-specific operands and parameters remain.
- `toolang` supports the same forms; usage/footer spellings follow the invoked
  executable. Shared option pointers and examples use the `too` alias.
- Success uses stdout and exit 0. Invalid topics/options/operands use stderr
  and exit 2. An unsupported AGENT prefix shows native help on stderr, exit 2.

## Reference patterns

| Reference | Relevant presentation |
| --- | --- |
| [Go: `go help environment`](https://pkg.go.dev/cmd/go#hdr-Environment_variables) | Topic prose followed by variable definitions; no help-command usage wrapper. |
| [GitHub CLI: `gh help formatting`](https://cli.github.com/manual/gh_help_formatting) | Formatting rules and examples directly, without helper-command flags. |
| [Git: `git help revisions`](https://git-scm.com/docs/gitrevisions) | A topic manual with its own description and syntax reference. |
| [curl: `curl --help http`](https://curl.se/docs/manpage.html) | A classified option list; its Usage describes curl execution. |

Use reference-only topic bodies. The directory explains how to request help;
a selected topic explains the referenced option or shared value grammar.

## `too help`

stdout; exit 0.

```text
Read shared option syntax.

Usage: too help [OPTIONS] [TOPIC]

Topics:
  allow    Select allowed resources
  limit    Set run limits
  default  Set runtime or Chat session defaults
  model    Select models and set model parameters

Options:
  -h, --help  Show this message and exit

Run 'too help TOPIC' for details.
Use 'too COMMAND --help' for command-specific options.
```

## `too help allow`

stdout; exit 0.

```text
--allow <KEY>=<VALUE>

Select allowed resources.

Keys:
  models    Model queries
  tools     Tool queries
  psyches   Psyche queries
  skills    Skill queries
  services  Service queries
  prompts   Prompt queries

  Repeat a key to combine TQ queries by union.

Values:
  QUERY  Match resources with a TQ query
  all    Select all resources
  none   Select no resources

  With all or none, that key may appear only once.
  Quote values containing spaces.
  Existing policy restrictions still apply.

Examples:
  Allow no tools
    too gus chat --allow tools=none

  Select OpenAI models
    too gus chat --allow 'models=openai/*'
```

## `too help limit`

stdout; exit 0.

```text
--limit <KEY>=<VALUE>

Set run limits.

Keys:
  agic_model_calls  Maximum model calls per agic
  agic_tool_calls   Maximum tool calls per agic
  tokens            Total input and output tokens per run tree
  cost              Total cost in USD per run tree
  time              Elapsed time in seconds per run tree

  Each key may appear once.

Values:
  INTEGER  Non-negative integer
  DECIMAL  Non-negative decimal; cost only
  none     Unset the limit

Examples:
  Limit tokens and cost
    too gus chat --limit tokens=100000 \
      --limit cost=1.5

  Unset the time limit
    too gus chat --limit time=none
```

## `too help default`

stdout; exit 0.

```text
--default <KEY>=<VALUE>

Set runtime or Chat session defaults.

Keys:
  model     Model value; see 'too help model'
  runnable  Entry ref, or none for automatic entry selection; Chat only

  Each key may appear once. Quote values containing spaces.

Applies to:
  serve, start   model; applies to the runtime instance
  chat           model and runnable; apply to the current session
  Script, rerun  Use --model for one run

  Chat defaults do not reconfigure the runtime or other sessions.

Configuration:
  Configuration uses [model]; legacy [default] tables are ignored.
  TOOLANG_DEFAULT_MODEL supplies runtime model defaults.
  TOOLANG_DEFAULT_RUNNABLE and runtime runnable defaults are unsupported; use
  driver handlers.

Examples:
  Set the session model
    too gus chat --default \
      'model=openai/gpt-5 effort=high'

  Select the session entry
    too gus chat \
      --default runnable=flow:review

See also:
  too help model  Model value syntax
```

## `too help model`

stdout; exit 0.

```text
Model selection

Syntax:
  --model <MODEL>          Script or rerun
  --default model=<MODEL>  Runtime or Chat defaults
  --compact-model <MODEL>  Compaction model for a new runtime

Model body:
  [ref=]MODEL [effort=VALUE] [max_output=VALUE]
  An exact model ref may be bare or use ref=. Put a bare ref first.
  Omit the ref to change parameters on an effective model.
  Quote multi-token values. Duplicate refs or parameters are rejected.
  --compact-model requires an exact ref; it rejects default, unset, and
  parameter-only values.

Parameters:
  effort      Reasoning level, non-negative token budget, or auto
  max_output  Positive output token count, or auto

  auto clears that parameter.
  Supported reasoning levels depend on the selected model.

Selection values:
  default  Use the inherited model or session baseline
  unset    Clear selection; startup entry fallback still applies

  In Chat and per-run overrides, unset selects model-free execution.
  none is not a model selection value.

Examples:
  Set a model with reasoning
    too gus chat --default \
      'model=openai/gpt-5 effort=high'

  Set the compaction model
    too start gus --compact-model \
      'openai/gpt-5 max_output=2048'

See also:
  too models        List available models
  too help default  Default setting lifetimes
```

## `too --help`

stdout; exit 0.

```text
Toolang — a language and runtime for agents and humans. (VERSION)

Usage: too [OPTIONS] <COMMAND> [ARGUMENTS]

Agent commands:
  new        Create an agent
  clone      Clone an agent
  remove     Remove an agent
  list       List agents and their status
  info       Show agent information
  cd         Enter an agent home

Cap commands:
  psyche     Manage psyche caps
  skill      Manage skill caps
  service    Manage service caps
  prompt     Manage prompt caps

Runtime commands:
  serve      Run an agent in the foreground
  start      Start an agent in the background
  stop       Stop an agent
  hub        Manage the hub service

Work commands:
  chat       Chat directly with an agent
  chore      Manage one-off and scheduled tasks
  talk       Have conversations with agents
  convo      Manage conversations and rules
  workspace  Manage agent workspaces

Inspection commands:
  caps       List available caps
  tools      List available tools
  models     List available models
  providers  List available model providers
  inspect    Inspect agent runs
  top        Monitor agent runs

Script commands:
  init       Initialize Toolang in a directory
  run        Execute a .too file

Options:
  -r, --root <PATH>  Use a custom Toolang root
  -V, --version      Show version and exit
  -h, --help         Show this message and exit

Run 'too more' to see additional commands.
Run 'too help' to see option help topics.
```

## `too gus chat --help`

stdout; exit 0.

```text
Chat directly with an agent.

Usage: too <AGENT> chat [OPTIONS]

Arguments:
  * AGENT  Agent name, path, ref, URL

Options:
  -t, --thread [THREAD]             Continue a thread instead of a new one
                                    [bare: latest thread]
      --catalog <PATH>              Use this model catalog
      --sandbox <SANDBOX>           Execute the session in this sandbox
      --allow <KEY>=<VALUE>         Select allowed resources; see 'too help
                                    allow'
      --limit <KEY>=<VALUE>         Set run limits; see 'too help limit'
      --default <KEY>=<VALUE>       Set session defaults; see 'too help default'
      --compact-model <MODEL>       Set the compaction model; see 'too help
                                    model'
      --enable <DRIVERS>            Enable only these drivers, replacing
                                    defaults: chat,talk,chore or none
  -w, --workspace [NAME=]<DIR>      Add a temporary workspace (repeatable)
  -d, --workdir [NAME=]<DIR>|<URI>  Set workdir; a path also adds a workspace
      --dev [PATH]                  Use a local Toolang wheel  [bare: .]
  -h, --help                        Show this message and exit
```

## `too serve gus --help`

stdout; exit 0.

```text
Run an agent in the foreground.

Usage: too serve [OPTIONS] <AGENT>

Arguments:
  * AGENT  Agent name, ref, URL

Options:
      --sandbox <SANDBOX>           Run in this sandbox; defaults to config,
                                    last successful selection, or host
      --allow <KEY>=<VALUE>         Select allowed resources; see 'too help
                                    allow'
      --limit <KEY>=<VALUE>         Set run limits; see 'too help limit'
      --default <KEY>=<VALUE>       Set runtime defaults; see 'too help default'
      --compact-model <MODEL>       Set the compaction model; see 'too help
                                    model'
      --enable <DRIVERS>            Enable only these drivers, replacing
                                    defaults: chat,talk,chore or none
      --catalog <PATH>              Use this model catalog
      --host <HOST>                 Bind the agent API to this host  [default:
                                    127.0.0.1]
      --port <PORT>                 Bind the agent API to this port
  -w, --workspace [NAME=]<DIR>      Add a temporary workspace (repeatable)
  -d, --workdir [NAME=]<DIR>|<URI>  Set workdir; a path also adds a workspace
      --dev [PATH]                  Use a local Toolang wheel  [bare: .]
  -h, --help                        Show this message and exit
```

## `too start gus --help`

stdout; exit 0.

```text
Start an agent in the background.

Usage: too start [OPTIONS] <AGENT>

Arguments:
  * AGENT  Agent name, ref, URL

Options:
      --sandbox <SANDBOX>      Run in this sandbox; defaults to config, last
                               successful selection, or host
      --allow <KEY>=<VALUE>    Select allowed resources; see 'too help allow'
      --limit <KEY>=<VALUE>    Set run limits; see 'too help limit'
      --default <KEY>=<VALUE>  Set runtime defaults; see 'too help default'
      --compact-model <MODEL>  Set the compaction model; see 'too help model'
      --enable <DRIVERS>       Enable only these drivers, replacing defaults:
                               chat,talk,chore or none
      --catalog <PATH>         Use this model catalog
      --host <HOST>            Bind the agent API to this host  [default:
                               127.0.0.1]
      --port <PORT>            Bind the agent API to this port
      --dev [PATH]             Use a local Toolang wheel  [bare: .]
  -h, --help                   Show this message and exit
```

## `too _serve gus --help`

stdout; exit 0.

```text
Run an agent server.

Usage: too _serve [OPTIONS] <AGENT>

Arguments:
  * AGENT  Agent name

Options:
      --allow <KEY>=<VALUE>    Select allowed resources; see 'too help allow'
      --limit <KEY>=<VALUE>    Set run limits; see 'too help limit'
      --default <KEY>=<VALUE>  Set runtime defaults; see 'too help default'
      --compact-model <MODEL>  Set the compaction model; see 'too help model'
      --enable <DRIVERS>       Enable only these drivers, replacing defaults:
                               chat,talk,chore or none
      --catalog <PATH>         Use this model catalog
      --host <HOST>            API bind host  [default: 127.0.0.1]
      --endpoint-host <HOST>   Externally visible endpoint host
      --port <PORT>            API bind port  [default: 7002]
      --log <LOG_SPEC>         Python logging specification
  -h, --help                   Show this message and exit
```

## `too gus rerun --help`

stdout; exit 0.

```text
Rerun an earlier run as a new one.

Usage: too <AGENT> rerun [OPTIONS] <RUN>

Arguments:
  * AGENT  Agent name, path, ref, URL
  * RUN    Run ID or thread ID; a thread selects its latest visible root run

Options:
      --catalog <PATH>       Use this model catalog
      --sandbox <SANDBOX>    Execute the new run in this sandbox
      --allow <KEY>=<VALUE>  Select allowed resources; see 'too help allow'
      --limit <KEY>=<VALUE>  Set run limits; see 'too help limit'
      --model <MODEL>        Override the model; see 'too help model'
  -i, --instance <INSTANCE>  Select a retained v… or r… instance; default:
                             latest startup
      --dev [PATH]           Use a local Toolang wheel  [bare: .]
  -h, --help                 Show this message and exit
```

## `too demo.too --help`

stdout; exit 0.

```text
Execute a runnable from demo.too.

Usage: too demo.too [OPTIONS] <RUNNABLE>

Runnables:
  greet agic  Agic greet

Options:
  -q, --quiet                       Hide run progress
  -o, --out <FILE>                  Save output to FILE; - for stdout
      --model <MODEL>               Set the model; see 'too help model'
  -w, --workspace [NAME=]<DIR>      Add a temporary workspace (repeatable)
  -d, --workdir [NAME=]<DIR>|<URI>  Set workdir; a path also adds a workspace
      --sandbox <SANDBOX>           Select the execution sandbox
      --allow <KEY>=<VALUE>         Select allowed resources; see 'too help
                                    allow'
      --limit <KEY>=<VALUE>         Set run limits; see 'too help limit'
      --dev [PATH]                  Use a local Toolang wheel  [bare: .]
  -h, --help                        Show this message and exit
```

## `too demo.too greet --help`

stdout; exit 0.

```text
Run agic greet.

Usage: too demo.too greet [OPTIONS] [ARGUMENTS]

Arguments:
  * name=<NAME>  Named input (Text)

Options:
  -q, --quiet                       Hide run progress
  -o, --out <FILE>                  Save output to FILE; - for stdout
      --model <MODEL>               Set the model; see 'too help model'
  -w, --workspace [NAME=]<DIR>      Add a temporary workspace (repeatable)
  -d, --workdir [NAME=]<DIR>|<URI>  Set workdir; a path also adds a workspace
      --sandbox <SANDBOX>           Select the execution sandbox
      --allow <KEY>=<VALUE>         Select allowed resources; see 'too help
                                    allow'
      --limit <KEY>=<VALUE>         Set run limits; see 'too help limit'
      --dev [PATH]                  Use a local Toolang wheel  [bare: .]
  -h, --help                        Show this message and exit
```

## `too help unknown`

stderr; exit 2.

```text
Error: No such command 'unknown'.

Try 'too help --help' for help.
```

## `too help chat`

stderr; exit 2.

```text
Error: No such command 'chat'.

Try 'too help --help' for help.
```

## `too help allow extra`

stderr; exit 2.

```text
Error: Got unexpected extra argument(s) (extra)

Usage: too help allow [OPTIONS]
```

## `too help --bogus`

stderr; exit 2.

```text
Error: No such option: --bogus

Usage: too help [OPTIONS] [TOPIC]
```

## `too gus help`

stderr; exit 2.

```text
Read shared option syntax.

Usage: too help [OPTIONS] [TOPIC]

Topics:
  allow    Select allowed resources
  limit    Set run limits
  default  Set runtime or Chat session defaults
  model    Select models and set model parameters

Options:
  -h, --help  Show this message and exit

Run 'too help TOPIC' for details.
Use 'too COMMAND --help' for command-specific options.
```

## Design review

- Corrected ambiguous `too help TOPIC --help` behavior: topics are native
  subcommands, so this displays the selected reference, not the directory.
- Removed the helper-command description/Usage/Options wrapper from successful
  topic pages. Single-option pages begin with the option declaration; the
  model page begins with its topic title and the relevant option forms.
- Corrected undefined table-label styling: definitions and descriptions
  now have separate semantic styles, reusing the existing CLI palette.
- Replaced generic Rules sections with subject-specific organization: key
  repetition under Keys, shared value forms under Values, and model notes
  beside model grammar, parameters, or selection values. Default values are
  key-specific and remain beside their keys rather than repeating a table.
- Corrected long example commands wrapping inside two-column tables: topic
  examples now use full-width, explicitly continued shell commands.
- Preserved the lifetime distinction: `default` explains runtime/session
  overrides; `model` owns body grammar and points back to default lifetimes.
- Kept default command help concise and complete; verbose references are
  requested explicitly. The model topic is longest because its accepted
  grammar and reset values need caller-specific restrictions.
- Existing formatting owns 44/80/120-column wrapping. Key identifiers may
  wrap at narrow widths; continuation descriptions remain in their column.
- No blocking design issue remains in these transcripts. Product behavior,
  offline side effects, import boundaries, and actual console compatibility
  still require the implementation acceptance checks in the parent plan.
