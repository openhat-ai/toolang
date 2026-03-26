# Toolang Chat Model

This document defines the canonical chat/message model for Toolang.

The goal is to keep one provider-independent message shape across:

- model providers
- streaming HTTP responses
- stored thread history
- future multimodal and tool-rich UIs


## 1. Canonical Message Model

Toolang should use one canonical message shape in code and storage:

- `TurnMessage`
- `MessagePart`

The direct ownership chain is:

- `thread`
  - groups turns under one durable subject
- `turn`
  - one handling attempt inside a thread
- `turn message`
  - one ordered message emitted inside a turn
- `message part`
  - one ordered content/tool/source/file unit inside a message

Recommended structure:

- `TurnMessage`
  - `id`
  - `role`
  - `parts[]`
  - `created_at`
  - `metadata`
  - `provider_metadata`

Supported core parts:

- `text`
- `reasoning`
- `tool`
- `source-url`
- `source-document`
- `file`

Rules:

- `parts[]` order is canonical
- tool invocations are stored as message parts, not in a side-channel array
- assistant messages may be tool-only and may contain no text part
- future multimodal content should extend `MessagePart`, not create a second
  message model


## 2. Stream Versus History

Toolang should treat streaming and history as two views of the same message:

- stream
  - the incremental construction of a message
- history
  - the final assembled `TurnMessage`

History should not persist text deltas or partial tool-input deltas.
It should persist the completed assembled message instead.

This requires one dedicated assembly layer:

- consume provider-independent runtime events
- emit wire-protocol chunks
- assemble the final `TurnMessage`

The mapping must not be reimplemented in multiple endpoints.


## 3. AI SDK Mapping

Toolang should align its wire protocol with the AI SDK UI Message Stream
protocol because:

- the current Web UI is AI SDK-oriented
- the protocol has already converged around cross-provider chat and tool usage
- it keeps stream semantics and stored message semantics close to each other

This alignment belongs in one explicit mapping layer.

Toolang should not expose AI SDK names as its internal canonical model.

Recommended split:

- internal canonical model
  - `TurnMessage`
  - `MessagePart`
- wire mapping
  - AI SDK-compatible chunks
  - AI SDK-compatible message JSON

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
  `toolCallId`, `toolName`, `inputTextDelta`, `errorText`
- internal dataclasses remain normal Python snake_case structures
- the snake_case to camelCase mapping belongs only in the protocol layer


## 4. Ordered Tool Parts

Tool usage must be part of the assistant message itself.

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


## 5. Future Multimodal Support

Multimodal support should extend `MessagePart`.

Do not add a second message protocol for images, files, or other media.

Expected future growth:

- image and audio inputs
  - represented as `file` parts with media metadata, or new specialized parts
- reasoning
  - represented as ordered `reasoning` parts
- citations and retrieved sources
  - represented as `source-url` or `source-document` parts
- generated files
  - represented as `file` parts

This keeps:

- model-provider adaptation
- stream adaptation
- persistence
- UI rendering

all centered on one concept system.


## 6. Recommended Migration Order

1. Add canonical `TurnMessage` and `MessagePart` dataclasses.
2. Add one AI SDK-compatible stream-mapping module.
3. Add one assembler that produces final `TurnMessage` values while streaming.
4. Move `/api/v1/chat/stream` to that mapping layer.
5. Move `/api/v1/chats/{thread_id}` to return ordered canonical message parts.
6. Replace chat storage that only stores plain text rows with storage that can
   persist final message parts.
