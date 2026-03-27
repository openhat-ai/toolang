# Toolang Chat Model

This document defines the canonical chat and message model for Toolang.

Chat is not a separate runtime hierarchy. It is a projection over:

- threads
- runs
- ordered messages

The goal is to keep one provider-independent message shape across:

- model providers
- streaming HTTP responses
- stored thread history
- future multimodal and tool-rich UIs


## 1. Chat Sits On Top Of Threads And Runs

Ownership chain:

- `thread`
  - groups related chat runs under one durable subject
- `run`
  - one chat handling attempt inside that thread
- `message`
  - one ordered chat message attached to that run
- `message part`
  - one ordered content, tool, source, or file unit inside that message

Rules:

- one chat send or reply creates one run
- `run_id` is the public link between runtime execution and chat history
- `turn_id` is not part of the public chat API


## 2. Canonical Stored Message Shape

The runtime API returns stored chat messages in this shape:

- `id`
- `thread_id`
- `run_id`
- `seq`
- `role`
- `parts[]`
- `created_at`
- `meta`

Supported core parts:

- `text`
- `reasoning`
- `tool`
- `source-url`
- `source-document`
- `file`

Rules:

- `parts[]` order is canonical
- assistant messages may be text-only, tool-only, or mixed
- future multimodal support should extend message parts rather than create a
  second message model


## 3. Ordered Tool Parts

Tool usage must remain part of the assistant message itself.

Do not return:

- `messages[]`
- `tool_calls[]`

and ask the client to reconstruct order.

Instead, one assistant message may contain ordered parts such as:

1. `text`
2. `tool`
3. `text`
4. `tool`
5. `text`

Each tool part should preserve:

- `tool_call_id`
- `tool_name`
- optional `tool_family`
- `state`
- `input`
- `output`
- `error_text`


## 4. Stream Versus History

Toolang treats streaming and stored history as two views of the same message:

- stream
  - the incremental construction of a message
- history
  - the final assembled persisted message

History should not persist text deltas or partial tool-input deltas.
It should persist the completed ordered message instead.

This requires one dedicated assembly layer:

- consume provider-independent runtime events
- emit wire-protocol chunks
- assemble the final persisted message

The mapping must not be reimplemented in multiple endpoints.


## 5. AI SDK Mapping

Toolang aligns its wire protocol with the AI SDK UI Message Stream protocol
because:

- the current Web UI is AI SDK-oriented
- the protocol has already converged around cross-provider chat and tool usage
- it keeps stream semantics and stored message semantics close to each other

Current target chunk families include:

- `start`
- `text-start`
- `text-delta`
- `text-end`
- `tool-input-start`
- `tool-input-delta`
- `tool-input-available`
- `tool-output-available`
- `tool-output-error`
- `finish`
- `error`

Rules:

- wire protocol uses AI SDK-compatible camelCase fields such as
  `toolCallId`, `toolName`, `inputTextDelta`, and `errorText`
- stored Toolang message objects remain normal snake_case payloads
- the snake_case-to-camelCase mapping belongs only in the streaming protocol
  layer


## 6. Thread Views

Thread list responses provide:

- `id`
- `kind`
- `title`
- `preview`
- `channel`
- `created_at`
- `updated_at`

Thread detail responses provide:

- `thread`
- `runs[]`
- `messages[]`

Rules:

- `title` is a stable thread title
- `preview` is the rolling latest-summary field
- `/api/v1/threads*` is the canonical surface
- `/api/v1/chats*` is a compatibility alias over the same thread data


## 7. Current API Guidance

`POST /api/v1/chat` returns:

- `thread_id`
- `run_id`
- `message`
- `assistant`

`POST /api/v1/chat/stream` returns SSE chunks for the same run and final
message.

`GET /api/v1/threads/{thread_id}` returns:

- thread metadata
- related runs
- ordered messages

The canonical public link between chat history and runtime execution is
`run_id`.
