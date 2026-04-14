# Toolang Chat Model

This document defines the canonical chat and message model for Toolang.

Chat is not a separate runtime hierarchy. It is a projection over:

- threads
- runs
- ordered step output

The goal is to keep one provider-independent message shape across:

- model providers
- streaming HTTP responses
- projected thread history
- future multimodal and tool-rich UIs


## 1. Chat Sits On Top Of Threads And Runs

Ownership chain:

- `thread`
  - groups related chat runs under one durable subject
- `run`
  - one chat handling attempt inside that thread
- `step`
  - one durable execution unit inside that run
- `message`
  - one caller-facing projection derived from one step output
- `message part`
  - one ordered content, tool, source, or file unit inside that message

Rules:

- one chat send or reply creates one run
- `run_id` is the public link between runtime execution and chat history
- `turn_id` is not part of the public chat API


## 2. Canonical Projected Message Shape

The runtime API returns projected chat messages in this shape:

- `id`
- `thread_id`
- `run_id`
- `step_index`
- `role`
- `parts[]`
- `created_at`
- `meta`

Supported core parts:

- `text`
- `tool_call`
- `tool_result`

Rules:

- `parts[]` order is canonical
- `user_input` steps project to `role="user"`
- `model_call` steps project to `role="assistant"`
- `tool_call` steps project to `role="tool"`
- future multimodal support should extend message parts rather than create a
  second message model


## 3. Ordered Tool Parts

Tool interaction should remain in the same part system as text.

Each tool part should preserve:

- `tool_call_id`
- `tool_name`
- `tool_family`
- `input`
- `output`

Rules:

- one assistant message may contain `text` and `tool_call` parts
- one tool step projects to one `role="tool"` message with `tool_result` parts
- clients should not reconstruct tool ordering from separate `tool_calls[]`
  side channels


## 4. Stream Versus History

Toolang treats streaming and projected history as two views of the same step
output:

- stream
  - the incremental construction of a message
- history
  - the final assembled projected message

History should not persist text deltas or partial tool-input deltas.
Durable truth should persist completed step output parts instead.

This requires one dedicated assembly layer:

- consume provider-independent runtime events
- emit wire-protocol chunks
- assemble projected messages from durable step output

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
- `finish`
- `error`

Rules:

- wire protocol uses AI SDK-compatible camelCase fields such as
  `toolCallId`, `toolName`, and `inputTextDelta`
- projected Toolang message objects remain normal snake_case payloads
- the snake_case-to-camelCase mapping belongs only in the streaming protocol
  layer


## 6. Thread Views

Thread list responses provide:

- `id`
- `title`
- `updated_at`
- `origin`

Thread detail responses provide:

- `info`
- `runs[]`

Rules:

- `title` is a stable thread title
- `origin` is the latest run origin visible on the thread
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

- thread info
- ordered run details

The canonical public link between chat history and runtime execution is
`run_id`.
