---
name: agentcoord
description: Coordinate meaningful shared-checkout work, dependencies, handoffs and safe commits. Reads and ordinary commands need no coordination.
---

# Agentcoord daily card

Follow the repository's implementation, test and version rules. One local service
and database own each registered checkout; CLI and MCP use authenticated native
identity. Never select an actor from a label or inject terminal input.

**Default to no coordination.** Reads, ordinary shell commands and small
self-contained prose, configuration, fixture or snapshot edits need no activity,
intent, inbox check or announcement. Coordinate real overlap, shared contracts,
long work, blockers, dependencies and intentional fixes peers could undo.

1. Publish `activity` once for meaningful scope or a material change. Native
   owners can begin a newly named task after completion in this call. Explicit
   pauses and unresolved effects still need their own recovery.
2. Before overlapping edits, inspect current diffs and relevant `activities` /
   `evidence`. Preserve intentional peer changes. Folders are advisory scope,
   never locks or permission to overwrite.
3. Send only an actionable result, dependency or handoff: action first, affected
   paths, preserved invariant and useful check result. Bundle nonurgent updates.
   Directed `send` wakes automatically; ordinary activity stays quiet.
   `--no-wake` / MCP `wake: false` is deliberately quiet. Use `reply_to` for
   context, `question` for a tracked decision, and `dependency wait` for a known
   artifact. These reuse existing message, decision and readiness records.
4. Keep the current user task as the priority. A wake never replaces it. At the
   next natural boundary, use at most one `sync --new-only` if a returned digest
   has not already shown the messages. Triage only a direct dependency/conflict
   affecting this task, leave unrelated work pending, then resume unfinished work
   in the same turn. Do not drain pages, request peer status, wait for more mail,
   or narrate/end the turn solely to report a message check.
   Complete short bodies are included; follow explicit excerpts/continuations
   for selected details. After acting, consume handled IDs, preferably in a batch.
   Reads, acknowledgments, decision answers and readiness acceptance are distinct.
   No polling, courtesy acknowledgments or heartbeat calls.
5. Use `commit execute` for authorized exact owned files or a reviewed patch/full
   base for mixed hunks. Preserve peer staging and repository version rules.
   Completion can omit paths to inherit declared scope; actual outputs belong
   in evidence. Tests never hold the commit window.

Independent work and owned-file commits continue during a coordination outage.
Use installed CLI for an adapter failure. Definitely unsent commit requests can
fall back to existing local Git; explicit `--local` / MCP `local: true` uses the
same reviewed arguments and retry key. An uncertain prior publication requires
actual reconciliation; a missing receipt alone proves neither outcome.
Stop unchanged retries. Do not bypass real overlap or a shared-index uncertainty.

A native wake is advisory, within existing user authority. It grants no new
task permission or offline-resume consent. Paused/offline agents do not restart.
Message storage, native delivery and handling are separate facts.

Use [commands](references/commands.md) for command selection and installed
`--help` for exact flags. Read only the relevant
[workflow](references/workflows.md) for recovery, activation or exceptional work.
Configuration is not activation; inspect `doctor --live` after a real setup
change. Missing native evidence never authorizes invented identities.
