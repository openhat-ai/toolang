# Distinguish Chat, Top, and Talk terminal titles

Status: approved for implementation by the human in the accompanying conversation.

## Goal and scope

Make OSC titles distinguish interactive Chat input, Top observation, and Talk
destinations. Change only Chat and Top display labels; retain Talk labels, CLI
commands, title prefixes, pane identities, and terminal publication lifecycle.

## Decisions

| View | Title without a configured prefix |
| --- | --- |
| Chat before a thread title is available | `gus>` |
| Chat with a thread title | `gus> hello world` |
| Root Top | `top` |
| Agent Top | `gus top` |
| Talk group | `#all` |
| Talk direct message | `@gus` |

Chat retains the existing thread-title source and lookup timing. The suffix is
the stored conversation title, not the current input buffer. Keep the existing
agent-name clipping, sanitization, complete-title width limit, prefix precedence,
and exit cleanup. Add one space after `>` only when a nonempty title is available.

## Touchpoints and acceptance

- Update `chat/title.py` and the Top command's terminal label; keep formatting at
  the existing CLI call sites.
- Update CLI design documentation and the changelog.
- Chat label and TUI tests cover startup, normalized titles, missing titles,
  fallback agent names, clipped names, asynchronous lookup, and TTY gating.
- Top command tests cover exact root and agent labels while preserving identities
  and cleanup; isolated tmux tests cover the resulting OSC payloads.
- Existing Talk tests retain exact group and direct-message labels.
- Run the repository's default verification and `git diff --check`.

## Risks and open questions

Consumers parsing display titles must accept the new spellings; structured pane
metadata stays unchanged. There are no open design questions.
