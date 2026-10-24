# ADR 0007: Expose the agent over MCP and A2A

- Status: Accepted; implemented (alpha)
- Date: 2026-10-24
- Deciders: Project owner

## Context

The reference runtime can place one outbound call from a command-line invocation and then
exit. That fits a scheduled personal call, but it does not let another agent or an LLM
host delegate a call, watch its progress, answer a question that arises during the call,
or cancel it. Exposing the call agent as a service turns the one-shot pipeline into a
long-lived runtime with a control surface.

Two kinds of client want that surface, and they are not the same:

- An **LLM host** with tool calling wants to invoke a capability and read the result. It
  speaks MCP, expects typed tool schemas, and drives a tool loop.
- A **requester agent** wants to hand over a long-running task, receive progress, supply
  missing input while the task runs, and collect a final artifact. It speaks A2A and
  expects a task lifecycle, not a single request/response call.

The voice plane is real-time: audio frames, endpointing, barge-in, and sub-second turns.
The control plane is asynchronous: a call can run for minutes, and a request that blocks
for the duration of a PSTN call is unusable to either client. MCP and A2A solve
different halves of the problem. MCP exposes tools to an LLM host. A2A delegates a
long-running task and supports mid-task input. Neither protocol alone covers both
clients.

There is one SIM and one audio path. A control plane that admitted concurrent calls
would promise capacity the hardware cannot provide, so both protocols must share one
queue and one concurrency limit.

## Decision

The runtime gains a long-lived `CallService` and two protocol adapters. The service owns
the FIFO task queue, the call lifecycle, consultation waits, in-memory task state, and
the optional JSONL audit log. It admits at most one active call. The adapters are thin
translations between protocol objects and service operations; they do not own call
state.

**MCP.** An MCP server over Streamable HTTP exposes `place_call`, `get_call`,
`answer_call`, `cancel_call`, and `list_calls`. `place_call` validates the objective,
destination format, and optional duration, enqueues a task, and returns a task id
immediately. It never blocks for the duration of the call. Status is read with
`get_call`. This is the path for an LLM host that treats the telephone as a tool.

**A2A.** An A2A server publishes a public Agent Card at `/.well-known/agent-card.json`
and serves the protocol 1.0 JSON-RPC methods `SendMessage`, `SendStreamingMessage`,
`GetTask`, `CancelTask`, and `SubscribeToTask`. A delegated call is an A2A task that
moves through `TASK_STATE_SUBMITTED` to `TASK_STATE_WORKING` as it is queued, dialing,
or in conversation; it becomes `TASK_STATE_INPUT_REQUIRED` while the agent waits for a
missing fact; and finally `TASK_STATE_COMPLETED`, `TASK_STATE_CANCELED`, or
`TASK_STATE_FAILED`. The final artifact is a structured summary plus the transcript.
Raw audio is never an artifact.

**Mid-call consultation.** The deliberative brain can emit `Consult(question)` when a
required fact is missing. The pipeline speaks (or logs, without a TTS adapter) a short
holding phrase, moves the task to `input_required`, and waits up to a bounded timeout for
an answer through either protocol. On a valid answer it resumes the conversation with
that fact. On timeout it speaks or logs a local fallback line and continues the call;
the callee is never left in unbounded silence. There is no question id. If an A2A consultation times out, the A2A
task stays `TASK_STATE_INPUT_REQUIRED` until the requester sends another message; there
is no server-initiated timeout update.

**Transport and trust.** Both listeners use one shared bearer token and bind to loopback
by default, or to the LAN only when explicitly configured and `[server].public_url` is
set. `/health` and the A2A Agent Card are public; the MCP and A2A RPC endpoints require
the token. The token is admin-level: there is no requester identity, per-token scoping,
destination allowlist, budget, or rate limit in v1. The objective supplied by a remote
agent is untrusted input: it defines the goal of the call, but no technical policy
boundary yet stops the model from acting on instructions inside it.

**v1 limits.** Tasks live in memory and are appended to a JSONL audit log; they do not
survive a restart. One call runs at a time. No raw audio is exposed over either
protocol. Calls remain outbound PSTN calls; only the control surface is new.

## Consequences

### Positive

- One queue and one task model serve both protocols, so queueing, cancellation,
  consultation, and audit behavior cannot diverge between MCP and A2A.
- LLM hosts can delegate a call with standard MCP tooling and no custom client.
- Requester agents get a standard task lifecycle, progress updates, and mid-task input
  instead of a bespoke polling API.
- `place_call` returns immediately, so the design fits request timeouts and tool loops
  while the call itself runs for minutes.
- The telephony path is unchanged. The dial helper, audio backends, and voice pipeline
  keep their existing responsibilities.
- The artifact is structured text, so a requester can act on the outcome without audio
  storage or a transcription step.
- Both adapters share one service, so an A2A-created call is visible through MCP and
  the same cancellation and consultation rules apply. A2A task ids remain separate
  from CallService task ids; an MCP-created call has no A2A task.

### Negative

- Two network protocol surfaces must be versioned, tested, authenticated, and monitored;
  MCP and A2A evolve independently.
- A2A task state and the internal call state must be reconciled. Divergence between them
  is a new class of bug.
- Tasks are in memory. A restart loses queued and active tasks, and a requester can lose
  a call it is waiting on.
- One concurrent call means head-of-line blocking: a long call delays every queued task,
  regardless of protocol.
- An `input_required` wait holds a live PSTN line and consumes call minutes without
  progress. The holding phrase is audible and slightly unnatural.
- A single shared token is coarse and admin-level. Every holder can read, answer, and
  cancel every task. There is no requester identity to scope access or budgets to.
- Transcripts in the artifact are personal data. They need authenticated delivery and a
  retention rule, and they widen the privacy surface compared with a summary alone.
- A remote objective and remote consultation answers are prompt-injection vectors. The
  runtime has no technical policy boundary outside the model yet; only the prompt asks
  the model to ignore conflicting instructions.
- The audit log is not transcript-free. Per-turn `transcript` and `reply` events carry
  text; only the final `finished` record drops the objective and full transcript.

### Neutral

- Calls remain outbound PSTN calls. Inbound PSTN calls stay out of scope, and inbound
  control-plane tasking is the only new inbound direction.
- The control plane carries text and task metadata, not audio. It does not turn the
  system into a voice-over-IP or media-streaming service.
- v1 has no push notifications. Clients poll or hold a stream open.
- The runtime now listens on a network socket, so firewall, TLS termination, and token
  rotation become operator concerns.
- The `serve` command starts both listeners; there is no per-protocol enable switch.
  They share the same service and single call slot, so enabling both does not add call
  capacity.

### Follow-up work (not implemented in v1)

- **Destination allowlist.** Validate the destination against an operator-managed E.164
  list or prefix set before a task is queued. This is a requirement, not a shipped
  control.
- **Requester identity and token scoping.** Map each token to a requester, own the task
  list by that identity, and allow one requester to see only its own tasks.
- **Budgets and rate limits.** Bound calls per requester per hour and per day, maximum
  duration, queue depth, and request rate. Refuse work over budget instead of queueing
  it without limit.
- **Durable task state.** A task store that survives a restart, so a requester does not
  lose a call it is waiting on.
- **A2A timeout updates.** A server-initiated update when a consultation times out, so
  the A2A task does not stay `TASK_STATE_INPUT_REQUIRED` until the next requester
  message.
- **Speech adapters.** The optional Grok Voice realtime adapter now provides a
  natural voice path; a bundled STT/TTS pair for the text pipeline remains future
  work.
- **Push notifications and TLS.** Both remain operator or deployment concerns. The
  runtime serves plain HTTP and relies on a reverse proxy or VPN for transport security.

## Alternatives considered

### MCP only

MCP is a tool protocol. It exposes typed tools and results, but it does not define
agent-to-agent delegation with a task lifecycle and mid-task input. A client would poll
a bespoke status tool, and the requester would have to be an LLM host with a tool loop.
A2A clients could not delegate a task, and there is no agent-to-agent artifact model. It
was rejected because it drops the agent-to-agent half of the problem.

### A2A only

A2A models a delegated task well, but most LLM hosts speak MCP rather than A2A.
Requiring an A2A client for a simple tool call raises integration cost and gives up MCP
discovery and typed tool schemas. It was rejected because it drops the LLM-host half of
the problem.

### Custom HTTP API

A single bespoke JSON API would be simpler to build and could cover queueing, polling,
answers, and cancellation. Every client would need a custom integration, the project
would own compatibility for an ecosystem it does not control, and there would be no
standard discovery or task model. It was rejected in favor of the two standard
protocols.

### stdio-only MCP

A stdio MCP server is spawned by the host on the same machine. That rules out a
long-lived service shared by requesters on the LAN and has no remote authentication
story; A2A needs an HTTP listener anyway. A stdio bridge for a local host could be added
later, but it is not the server transport.

## References

- [Interoperability guide](../interop.md).
- [Architecture](../architecture.md), control plane section.
- [Design brief](../design-brief.md), sections 1, 2, 10, and 12.
- [Voice agent](../voice-agent.md), call lifecycle and timeouts.
- [ADR 0005: Originate from a real SIM](./0005-originate-from-real-sim.md).
- [Model Context Protocol specification](https://modelcontextprotocol.io/specification/).
- [Agent2Agent protocol](https://a2a-protocol.org/).
- [Security policy](../../SECURITY.md).
