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

Scheduled native resumes require explicit opt-in, supported native target and
verified offline execution identity. Possible external effects whose outcomes
are uncertain require reconciliation; elapsed leases never justify a second
launch. Cancellation signals only owned launched process groups. User agent
sessions are never terminated by service installation or upgrade.

## Installation and operator inspection

`init` writes optional project configuration and narrowly marked instruction
snippets, preserving existing user content. `init --candidate-dir PATH` produces
inert MCP/lifecycle/Herdr fragments for review. Merge them deliberately and reload
the harness at a natural boundary. Native CLI remains available while an existing
MCP client retains its old discovered tool catalog.

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
