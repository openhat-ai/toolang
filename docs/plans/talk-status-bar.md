# Talk conversation interface

Approved behavior for `too talk`.

## Goal and vocabulary

Talk reads and writes a messaging **conversation**. A conversation has a canonical
`group:<id>`, a kind (`direct` or `group`), members, and optional display metadata.
A direct conversation has two fixed participants; a custom group's membership can
change. Hub manages the public `group:all`. Use `convo` for abbreviated metadata.
[Chat](../chat.md) runs agent work within an execution **thread**.

## Command and message flow

- `too talk` lists conversations. `too talk TARGET` opens an interactive view;
  `too talk TARGET MESSAGE...` sends once and exits with a receipt or error.
  Targets and `--dm`/`--group` follow the [messaging contract](../messaging.md).
  Arguments after the target are literal message text.
- Resolve the target to its canonical ID through the running Hub. Load that
  conversation's metadata, show retained history, then follow incoming messages
  using an independent Stream-ID cursor. Transient read failures resume from the
  committed cursor with backoff; terminal failures display their error and stop
  the receive loop. The message loop reads only this conversation.
- The viewer is Hub's configured human. Members can send; observers see messages
  without an input box. Membership and display metadata are loaded on entry.
  The view pins its Hub connection and viewer until the user opens a new view.
- The input placeholder is `write a message`. Enter sends, Ctrl+J inserts a
  newline, Ctrl+P/Ctrl+N browse sent input, and Ctrl+Q exits. Failed sends preserve
  the draft and display the returned error. Sends are never retried automatically;
  the user decides whether to retry or reopen. Drafts and input history remain
  under `.runtime/text/`, scoped by root, backend, viewer, and conversation.

## Presentation

The normal footer has two-cell side insets aligned with the input text. Its
content width follows Chat's limit: 120 cells by default, configurable with
`TOOLANG_PROGRESS_MAX_WIDTH`, and capped by the terminal width.

| Segment | Content |
| --- | --- |
| Left, direct | `@` plus the other participant names in stable comma-separated order, excluding the viewer. An observer sees both names. |
| Left, group | `#` plus the conversation's display name and total member count, such as `#dev(3)`. Preserve the name as supplied. |
| Center | Complete canonical conversation ID, centered by terminal cells. |
| Right | Plain viewer login name; `Connecting…` during initial connection. |

Only the `@` or `#` marker dims when the viewer cannot send. Names and counts use
normal foreground. The interactive view does not display presence. On narrow
terminals, prioritize the login, hide an ID that cannot fit intact, then truncate
the left label. Reduce the insets only below five cells; never wrap the footer.

Send, draft, and terminal receive errors replace the entire footer using Chat's
red `!` row, with the marker in the first column and a two-cell trailing inset.
Show the actual error detail, including Hub configuration failures. A transient
receive interruption shows `Reconnecting…` in this row while retrying the read.
Successful sends leave the normal identity row visible.

Messages use terminal scrollback. Agent bodies reuse Chat's Markdown renderer;
human bodies remain literal text. Names and markers share a header row aligned
with body text. Agent names and markers use the same stable ANSI color derived
from the name, without dimming. A faint dashed rule appears above each agent header.
Left messages fill the available width; own messages align right. See the
[message layout contract](talk-agent-header-divider.md).

## Terminal identity and placement

Publish the compact conversation label as a sanitized OSC 0 title on interactive
TTYs: `@alice`, `@alice,bob`, or `#dev`. Clear it on exit. Terminal settings control
how the title appears; title publication is independent of tmux placement.

Inside a tmux server, all Talk windows share the session named `talk`. Create it
only when absent. Each conversation and connection context has a reusable window;
its initial name is the canonical ID. Resolve windows through their metadata even
when the user renames them. Preserve other windows and shells in the session.
A Talk pane is managed by the launcher and can be reopened after it exits.
`TOOLANG_TMUX=0` runs Talk in the invoking terminal.

| Metadata | Scope | Value |
| --- | --- | --- |
| `@toolang_talk` | Session | `talk` |
| `@toolang_convo` | Window | Canonical conversation ID |
| `@toolang_context` | Window | Hash of root, backend, viewer, and Hub endpoint |
| `@toolang_pad` | Pane | `talk` |

## Acceptance and ownership

| Scenario | Pass condition |
| --- | --- |
| Command and targeting | Help, directory, interactive view, one-shot send, typed targets, and literal arguments match the messaging contract. |
| Membership | A member can send; an observer sees the same messages with a dim permission marker and no composer. |
| Footer | Direct/group names, total members, login, Unicode widths, and complete centered IDs fit the configured width. |
| Errors | Terminal read, send, and draft errors show their detail in Chat's full error row; failed sends retain drafts and make one request. |
| Receive recovery | Resume from the last displayed cursor without replaying history or requesting full directories. |
| Rendering | Markdown, sender colors, header alignment, dividers, and message widths satisfy the message layout contract. |
| Titles | TTY titles identify the conversation and clear on exit; redirected output emits no OSC. |
| Placement | Conversations and contexts share one session; renamed windows reuse their panes; reopening after child exit or a Hub port change reaches the correct view. |

Talk's CLI owns resolution and placement, its TUI owns input and message following,
and `talk/status.py` owns labels and footer geometry. `common/status.py` owns the
shared error row; `common/tmux.py` owns placement. Hub retains permission and
configuration checks. Run the repository's default checks and isolated tmux tests.

Limits: metadata reflects entry time, narrow screens may hide the canonical ID or
clip error detail, and terminal settings may suppress titles. No open questions.
