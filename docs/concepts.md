# Overview

This document defines the runtime terms used across Toolang.


## Agent

An agent is one local runnable program and its owned state.

Each agent has:

- one source program
- one cap set
- one job set
- one runtime room


## Caps

Caps are reusable agent primitives that shape behavior and available tools.

Current cap kinds are:

- `psyche`
- `skill`
- `service`
- `prompt`

Caps are definitions. They are not runs.


## Jobs

Jobs are durable authored work definitions.

Current built-in job kinds are:

- `task`
- `chore`

The jobs API also exposes one `will` endpoint for a long-horizon definition.
When no will is configured, that endpoint returns `null`.

Jobs are definitions. They are not runs.


## Thread

A thread is a durable execution context.

A thread groups related runs under one stable topic or work item.


## Run

A run is one concrete handling attempt inside one thread.

A run has:

- one origin
- one input message
- one status
- zero or more steps


## Step

A step is one execution unit inside one run.

Current step kinds are:

- `model_call`
- `tool_call`
- `runtime`

Steps record execution truth. They do not define transport behavior.


## Message

A message is the canonical content unit used across:

- run input
- model calls
- projected thread history
- streaming chat responses

Each message has:

- one role
- ordered `parts`
- optional `meta`

Current core part kinds are:

- `text`
- `tool_call`
- `tool_result`


## Relationships

Toolang uses these ownership rules:

- one agent owns caps and jobs
- jobs and chat inputs create runs
- runs belong to threads
- runs contain steps
- step output projects to caller-facing messages

This keeps authored state, execution truth, and transport output separate.
