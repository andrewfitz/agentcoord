# Agentcoord protocol

Agentcoord coordinates native agents working in one shared Git workspace. One
installed service owns its private local database. CLI, MCP and operator views
use the same typed operations. A separate checkout is a separate workspace;
symlink aliases resolve to the canonical workspace. Relocation requires an
explicit registry update.

## Daily work

Publish activity once when starting or materially changing meaningful scope,
waiting on a real dependency, handing off or completing work. Record intent,
invariants and useful evidence. Folder scopes help discovery and grant no edit
reservation. Inspect relevant activity and current diffs when overlap matters.
Preserve intentional peer edits and continue independent work while waiting.

At a natural work boundary, `sync` presents a bounded pending action view. Read
full message or decision content by ID when its preview is insufficient. Explicit
message consumption records handling. Decision resolution records an answer,
decline or requester cancellation. Readiness acceptance records a reviewed
dependency. These transitions are separate; a read never performs them.

Message bodies use explicit UTF-8 byte chunks. Message details include a bounded
attachment summary; `attachments MESSAGE_ID` traverses the complete attachment
index with `--after`. `attachment ATTACHMENT_ID --offset N` retrieves the complete
retained JSON reference in byte chunks. Follow `next_offset` and join the returned
`reference` strings before parsing JSON. These reads use the message's sender,
recipient or operator visibility and never handle its content.

Use plain-language messages that change the recipient's next action: the result
or question first, then relevant paths, ownership and enough evidence to act.
Do not announce ordinary reads/commands, repeat available status, send courtesy
acknowledgment chains or broadcast unrelated updates. Bundle related nonurgent
deltas for one recipient; deliver an urgent blocker promptly. Preserve intentional
fixes by naming their purpose and conditions. Send receipts never echo body text.

Activity discovery defaults to current reported scopes; explicit `current=false`
or CLI `--no-current` traverses retained history. Neither view grants ownership or
proves presence. Keep ordinary spacing and meaningful names in message bodies;
omit duplicate identity envelopes and link long evidence instead of copying it.

Current scope discovery excludes archived actors and obsolete canonical task
assignments. The operator's current work view uses the same definition; its all
view retains historical scopes. A materially new activity scope returns up to
three advisory peer scopes in `scope_context`. This sends no messages and grants
no locks. Folder overlap alone needs no question, permission or wait.

For a non-obvious settled fix that peers might undo while your larger task is
still running, record one scoped `intent` with its purpose and preservation
conditions. Mark that intent completed when the fix settles; this does not complete
the actor's task. Use activity/readiness evidence for its test or commit receipt.
Skip intent records for routine edits whose reason is already clear. No message
is needed unless a specific peer must change their next action.

`evidence --paths ... --limit 3` also returns recent completed `outcomes`, newest
first, even after their author changes tasks or becomes historical. Continue
older outcomes with `--outcomes-before`; evidence pages allow at most 20 records
per section. Before replacing an intentional fix, retrieve the relevant activity
detail and inspect current source. Preservation conditions are useful context,
not permanent veto power over justified subsequent fixes.

Activity discovery and mutation receipts are bounded previews. `note_excerpt`,
`evidence_omitted`, `paths_more` and byte/count fields identify omitted content;
never treat a preview as the complete scope or evidence. Use
`evidence-detail --kind activity --id ...`, following `paths_after` with
`--paths-after` for complete path pages. Readiness details retain complete evidence.
Repeating an identical intent does not append another event or change its timestamp.

Selected messages may be read together with `message-batch`. The bounded response
has explicit `next_index` over the same ID list and each body's `next_offset`;
neither reading nor pagination handles messages. Previews expose sender, subject,
thread and body bytes, and mark incomplete summaries. CLI/MCP JSON omits optional
formatting whitespace while preserving all content, fields and recovery receipts.

Before creating a blocking request, find relevant open decisions and follow one
that covers the actual dependency. Followers acquire no answer or edit authority.
Deadlines make a decision overdue; silence never resolves it or transfers work.
An answer from an old task generation is a proposal requiring explicit current
authority reconciliation.

Publish readiness only after preparing its complete counterpart scope. Receipts
bind producer generation, exact paths, hashes and opaque evidence references.
Supersede or withdraw obsolete receipts. Consumers inspect and accept the latest
complete receipt; unrelated commits do not invalidate unchanged inputs. Evidence
references remain data and are never executed by the service.

## Identity and execution

Bindings use actual harness session and child identity. Reconnect preserves actor
and task identity. Task assignment, connection and native execution generations
are separate values. A shared MCP connection represents one caller; independent
children require their own native binding. Display labels and active panes do
not select authority.

`identity` returns its `workspace_id` and independently bound actor. Delayed
operations may pass global `--origin-context` with exactly `workspace_id`,
`actor_id`, `task_generation` and `execution_generation`, all canonical UUID
strings with a non-null execution. Native binding still supplies identity;
the snapshot only restricts it. Another workspace or actor is rejected, and
changed task/execution generations cannot acquire current authority. The
original generation guards remain fixed across calls and retries. Ordinary
calls retain their current binding behavior.

Lifecycle events observe native presence. Events lacking originating execution
correlation remain uncertain observations and cannot release protected execution,
complete work or authorize resume. Reported task state, observed presence and age
remain separate in operator views. Agents send no heartbeat messages and ordinary
commands need no coordination hooks.

Lifecycle hook payloads require an absolute `cwd`, `workspace_root` or
`project_dir`. Each supplied path must belong to the selected workspace in its
registered state location. Ordinary subdirectories are accepted; a separately
registered nested workspace belongs to its own service. Missing, invalid or
foreign paths are ignored before opening a native connection. The configured
service route does not establish the event's originating workspace.

## Transactions and retries

Messages, explicit recipients and linked decision/readiness notifications commit
atomically. Actor-scoped retry keys bind canonical payload hashes. Retrying the
same key and payload returns its committed receipt; changing the payload fails.
After a timeout, inspect the retained operation or retry the same key. Durable
acceptance does not guarantee presentation, handling or native process execution.

Git execution selects exact paths or a reviewed patch and full base. Disjoint
preparation proceeds concurrently; the final shared Git update is serialized.
Unrelated staging and peer hunks are preserved. A published commit remains
published even when a later notification or cleanup fails. Inspect its receipt
before another attempt. Commits do not submit or await tests.

Coordination outages do not veto independent edits, checks or authorized commits.
An adapter-only failure can use the installed CLI. An unreachable service stops
coordination calls, not useful work: for reviewed fully owned paths, direct
`git commit --only -m MESSAGE -- PATH ...` preserves unrelated staging under Git's
normal locks. Add only exact owned new paths first when needed; retain local
version rules and existing hooks. Never bypass real conflicts or switch commit
mechanisms while a prior publication is queued, running or uncertain. Defer only
the dependent overlap, retain a short local outcome receipt, then publish one
summary after connectivity returns. No polling or repeated status/message calls.

For `UNBOUND_ACTOR`, inspect the native binding/setup failure once. The installed
CLI is useful only when its supported native context is available; guessing
session IDs, adding labels or repeating environment variants cannot repair missing
proof. Correct a confirmed hook/trust/configuration cause before retrying. If that
cannot be done in this session, continue independent work and the owned-file Git
outage procedure. Record the binding limitation once; do not start a diagnosis
loop or refuse safe work because it cannot be messaged.

Repeated identical ambiguous lifecycle observations retain their first event and
remain uncertain. They cannot complete work, release grants or prove absence.
Verified offline sessions with no bindings, unsettled operations, protected commit
grants or pending scheduled jobs leave current-scope indexes. Retirement does not
complete their tasks or grant overwrite/resume authority; history and pending
records remain. Unknown presence is not evidence for retirement.

Scheduled native resumes require explicit opt-in, supported native target and
verified offline execution identity. Possible external effects whose outcomes
are uncertain require reconciliation; elapsed leases never justify a second
launch. Cancellation signals only owned launched process groups. User agent
sessions are never terminated by service installation or upgrade.

## Installation and operator inspection

`init` previews setup; `init --apply` registers the workspace, writes optional
project configuration and marked instruction snippets, installs the shared
`.agents/skills/agentcoord/SKILL.md` and safely merges selected harness project
MCP/lifecycle configuration. `--harnesses` selects Claude, Codex, Cursor and Grok,
with all selected by default. Existing user content and unrelated tools/hooks
are preserved; installer-owned resources changed by the user are refused rather
than overwritten. Equivalent already configured native lifecycle handlers may
be reused to avoid duplicate observations. Installation does not start a service
or authorize native resume scheduling.

AGENTS.md routes coordination to the shared skill and CLAUDE.md imports AGENTS.md.
Claude/Cursor native skill pointers refer to that one authority; Codex/Grok
discover it directly. The skill's progressively loaded command/workflow
references cover coordination only; repository and user rules still own scope,
implementation, checks and commits. `init --candidate-dir PATH` produces inert
configuration, skill and instruction candidates for review.

Review full lifecycle hook definitions, approve changed definitions through the
harness's native hook-trust mechanism, and confirm the actual client loaded and
ran them; parsed configuration alone is not proof. An unsupported approval
mechanism remains an explicit operator-review step. Hooks and MCP catalogs may
be cached until a natural session reload/reconnect. Refresh clients before
retiring old commands. Existing MCP sessions use the installed `agentcoord` CLI
with `--project /absolute/repository` until their catalog refreshes; never invoke
cached retired tools. Do not inject terminal input or force-restart agent sessions.

macOS supports per-workspace launchd units. Linux supports an independently usable
foreground service with `agentcoord serve`. Managed restart/removal requires the
owned service to drain and confirm quiescence. Pending jobs and retained history
survive. Foreign units, unknown service health and uncertain effects fail
explicitly. The installer never activates unrelated services or force-kills
agent sessions.

The read-only monitor exposes current actors, canonical pending actions, scoped
work, dependencies, jobs, failures and searchable paged durable history. Operator
reads never present or consume agent messages. `doctor` separates configuration,
connection, binding, database health and observed lifecycle; configured or
reachable does not prove every open harness has reloaded its integration.

The local OS user is the trust boundary. Private state/socket permissions and
native capabilities prevent accidental cross-session or cross-workspace access;
they do not sandbox arbitrary malicious processes owned by that same user.
