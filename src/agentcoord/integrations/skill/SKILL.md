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

- Publish `activity` once when starting or materially changing coding scope,
  a long test/audit, a blocker, a handoff or completion. Record useful intent,
  paths and evidence; paths help discovery and do not reserve edits.
- Before overlapping edits, inspect the current diff and relevant `activities`
  and `evidence`. Preserve intentional peer fixes. Ask the responsible owner
  only when a real dependency or shared contract needs a decision.
- Find an existing covering decision with `request-find` before creating one.
  Continue independent work while waiting. Silence, a deadline or an offline
  actor never authorizes overwriting, transfers authority or resolves a request.
- At natural work boundaries, handle returned action digests or use `sync`.
  Read full content when needed. Message handling, decision resolution and
  readiness acceptance are separate explicit transitions. Do not poll an inbox,
  send heartbeats or wrap reads and ordinary commands in coordination calls.
- Publish complete artifact counterparts with `ready` or `handoff` and useful
  evidence. Consumers inspect current hash-bound readiness before accepting an
  update and integrating dependent work.
- When a commit is authorized by the user or repository rules, use native exact
  paths for owned files or a reviewed patch for owned hunks in mixed files.
  Preserve peer edits and unrelated staging. Tests do not hold the commit window.
- A coordination outage does not block independent edits, checks or authorized
  commits of fully owned files. Read the outage section below for direct Git
  fallback; a failed message is not a missing commit permission.

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
