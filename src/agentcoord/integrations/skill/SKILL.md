---
name: agentcoord
description: Coordinate meaningful work, dependencies, artifact handoffs and safe commits among agents sharing a Git checkout using Agentcoord. Use for coordination setup or recovery, overlapping edits, pending actions and scheduled work; ordinary reads and shell commands need no coordination.
---

# Agentcoord

Use the installed Agentcoord CLI or current MCP adapter. Both use one local
service and private database for the registered workspace. This skill describes
coordination, not the repository's implementation, test or version policy. Follow
local instructions and the user's authorized scope for those choices.

## Meaningful work

Use plain language and make the recipient's next action clear. Send only a real
overlap, decision, changed contract, blocker or usable handoff. Put the action or
result first, then the affected path and evidence needed to act. Omit narration,
repeated context, acknowledgment chains and requests for status already recorded.
Batch related nonurgent updates for the same recipient; do not delay a blocker.

Default to no coordination. A small self-contained edit to prose, Markdown,
README text, translations, fixtures, snapshots or ordinary configuration needs
no `activity`, `intent`, message, status lookup or inbox check, including at
start and completion. A tracked file is not automatically shared work. Use
coordination only for exact-file overlap, a shared API/schema/generated
contract/instruction change, a long test or audit, a blocker, a handoff, a real
decision, or a non-obvious fix that another agent could reasonably undo. If none
applies, edit and verify silently.

- Publish `activity` once when starting or materially changing coding scope,
  a long test/audit, a blocker, a handoff or completion. Record useful intent,
  paths and evidence; paths help discovery and do not reserve edits.
  Supply a concrete task name on first use (`--task` in CLI); later calls can
  reuse the assigned task. This belongs in the scope call, not a separate handshake.
- Before overlapping edits, inspect the current diff and relevant `activities`
  and `evidence`. Preserve intentional peer fixes. Ask the responsible owner
  only when a real dependency or shared contract needs a decision.
- Find an existing covering decision with `request-find` before creating one.
  Continue independent work while waiting. Silence, a deadline or an offline
  actor never authorizes overwriting, transfers authority or resolves a request.
- At natural work boundaries, handle returned action digests or use `sync`.
  Read full content when needed. Message handling, decision resolution and
  readiness acceptance are separate explicit transitions. Consume selected messages
  after acting, not just reading; use one batch for a handled set. Do not poll an inbox,
  send heartbeats or wrap reads and ordinary commands in coordination calls.
- Publish complete artifact counterparts with `ready` or `handoff` and useful
  evidence. Consumers inspect current hash-bound readiness before accepting an
  update and integrating dependent work.
- When a commit is authorized by the user or repository rules, use native exact
  paths for owned files or a reviewed patch for owned hunks in mixed files.
  Preserve peer edits and unrelated staging. Tests do not hold the commit window.
- A coordination outage does not block independent edits, checks or authorized
  commits of fully owned files. `commit execute` automatically falls back to
  verbose local Git for a definitely unsent request; use the same arguments and
  key with `--local` / MCP `local: true` explicitly when needed. Read the outage
  workflow for recovery; a failed message is not a missing commit permission.

A new scope's `scope_context` is advisory; folder overlap alone needs no message
or wait. `evidence` includes recent completed outcomes after owners change tasks.
Read selected details when previews mark omitted evidence, note or paths.

For a non-obvious settled fix that peers might undo before your task finishes,
record one scoped `intent` with its purpose/invariants. Completing an intent does
not complete the actor's task. Routine edits need no extra records or messages.

For missing native binding, inspect the failure once and correct a confirmed
setup/trust cause before retrying. Do not guess identities or try repeated
environment variants. If proof remains unavailable, continue independent work
and the documented safe Git outage workflow; report the limitation once.

## Choose the needed reference

- Read [references/commands.md](references/commands.md) to choose a command;
  it covers every command family and when to use it. Use the installed command's
  `--help` for flags and current schemas, rather than loading all references.
- Read [references/workflows.md](references/workflows.md) for overlap decisions,
  handoffs, commit ownership, coordination outages, retry recovery, scheduled work or integration
  activation. Its setup section explains lifecycle hooks and reload/trust checks.
- Agentcoord's package `protocol.md` owns the detailed protocol. Installed help
  owns command syntax; report a mismatch instead of inventing compatibility.

## Native identity and recovery

Run inside the workspace or pass global `--project /absolute/repository` before
the subcommand. `identity` reports the bound native actor. A shared MCP connection
represents one caller; children need their own native binding for independent
attribution. Never fabricate identities, use display labels as authority or
inject terminal input. Lifecycle hooks observe native presence; task state and
observed presence remain separate.

Mutations accept a stable `--key`. Retain it across uncertain responses and use
`receipt` or `operation get` before repeating an effect. A queued result is not
completion. Resume scheduling requires explicit user opt-in and a supported,
verified offline native target; installation does not grant that consent.

After an integration change, parsed configuration does not prove activation.
Approve changed hook definitions through the harness's native trust mechanism
and confirm the client loaded and ran them. Existing MCP sessions may retain an
old catalog: use the installed CLI until a natural reconnect refreshes it, without
force-restarting sessions or invoking retired cached tools. Use `doctor --live`
for diagnosis. The local OS user is the trust boundary.

## Native wake signals

Use ordinary `send` for an actionable dependency or handoff. Native attention is
automatic; agents need not inspect recipient activity or select a wake flag.
Use `--no-wake` / MCP `wake: false` for a deliberately quiet message. Never
choose a recipient harness/session or inject terminal input. On a native wake
signal, run one `sync`, read relevant messages, act within
existing authority and consume handled messages. No courtesy reply or polling.
Unavailable delivery never blocks independent work. Consult the workflow reference
for one-time repository policy and native Channel/shared-daemon activation.
