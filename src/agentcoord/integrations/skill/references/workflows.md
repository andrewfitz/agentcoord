# Coordination workflows

Use the section needed for the current task. The installed command's `--help`
supplies exact flags; [commands.md](commands.md) selects commands by purpose.
Agentcoord's package `protocol.md` is the protocol authority. Repository policy
still owns placement, testing, commit expectations and versioning.

## Scope, overlap and decisions

### Silent-by-default rule

Do not call Agentcoord for a small, self-contained text or documentation edit.
That includes README and Markdown wording, translations, fixtures, snapshots,
ordinary configuration, and their normal verification. Do not announce the
start, each write, each read, or completion. Do not run `identity`, `status`,
`activities`, `evidence` or `sync` as ceremony. A path being tracked, a broad
folder scope, or another agent being online is not a reason to coordinate.

Use one activity only when work is long-running or materially shared, or when it
records a real blocker, handoff or completed outcome. Use `intent` only for a
settled non-obvious fix that could be reverted. Use a message or request only
when a named recipient must change an action or answer a dependency. A document
change becomes coordination-worthy when it changes shared agent instructions,
an API/schema/generated contract or an acceptance rule relied on by other work;
ordinary prose cleanup remains silent. When in doubt, stay silent and keep
working unless the exact file is actively overlapping.

Publish one `activity` for meaningful starting scope and update it only when the
scope or outcome materially changes. Include a task name in the first activity;
a fresh native session has no assigned task yet. No separate identity/status call
is required merely to start work. Before editing overlapping work, read the
current Git diff and relevant `activities --paths ...`/`evidence` (current scopes
are the default). Use `activities --no-current` only when an earlier intentional
change needs explanation.
Scope history is a discovery
lead, not a lock or proof of live ownership. Preserve intentional repairs,
including changes not related to your task.

Use existing evidence before asking an owner. If a real dependency remains,
`request-find` can reveal an open covering decision: follow it instead of creating
a duplicate. Otherwise create one targeted `request`, with the needed decision
and affected paths. Continue independent work while waiting. Use a normal `send`
for an actionable nonblocking message; local repository work does not authorize
unrelated external messages.

Only explicit authorized resolution answers, declines or cancels a request.
Consumption, acknowledgment, a deadline, silence and offline presence do not.
Use `request-defer` when deferring the dependent slice, and `request-transfer`
only with current scoped authority. A response from an old task generation is
a proposal: inspect the exact proposal with `request-get` and reconcile it
explicitly under current authority before treating it as a decision.

A materially new scope returns a small advisory `scope_context`; use it instead
of making an extra discovery call when it already supplies enough context.
Folder overlap alone is not a conflict. `evidence` includes recent completed
outcomes after their owners move on; inspect the selected activity detail before
reverting a deliberate fix. Follow explicit path cursors and omitted-content flags.

## Preserve a deliberate fix during a longer task

If a non-obvious settled fix could be undone by overlapping work, publish one
`intent` for the relevant paths with its purpose and invariants. Completing the
intent does not complete your actor's task. This retains the reasoning after your
activity scope moves on; use activity/readiness evidence for checks or commits.
Routine edits need no extra intent record. Notify a peer only if they must change
an action, rather than announcing the fix to every agent.

## Useful messages

Send when the recipient needs to change an action or answer a real decision.
Independent work needs an activity record, not a round of messages. Use ordinary
language; no code dictionary, rigid form or routine acknowledgment is required.
A few sentences usually suffice, but retain essential conditions and evidence.

- Lead with the requested action, settled decision or usable result.
- Name the exact shared path/interface, intentional behavior to preserve and
  ownership boundary when overlap matters. Explain a deliberate fix so a peer
  can avoid reverting it.
- Include a retained test/request/commit reference and its actual outcome when
  it supports the action. Link long logs and reports instead of pasting them.
- For a question, state the choice and consequence once. Use a tracked request
  only when that answer really gates a dependent slice; continue other work.

Examples:

> Preserve the escaping fix in `src/parser.py`; quoted commas must stay in one
> token. Regression passed in request `<actual-id>`. I own tokenization; your
> rendering changes can continue independently.

> Is the new endpoint's `region` required? I need that decision for validation.
> Current schema allows omission; making it required changes the app payload.
> I am continuing the unrelated retry fix while this request is open.

Reuse the thread and existing decision. Bundle related nonurgent deltas for the
same recipient. Send an urgent blocker or contract change immediately to affected
owners, not every agent. Do not send read/command announcements, repeated scope
updates, courtesy acknowledgments, or ask for evidence already available. A sent
message is delivery, not a promise that the recipient has acted.

Keep normal spaces and precise names. Brevity comes from removing repeated
context, not squeezing words together. Native sender, thread and paths already
carry routing metadata; do not paste another identity/JSON envelope into the body.
Quote only the evidence needed for the decision; use a retained reference for
long logs or lists of hashes.

For generated outputs or evolving shared contracts, use a readiness subscription
and publish all counterparts together. Inspect its current receipt instead of
repeatedly asking whether source is settled. Reproducible reads can use retained
source versions; ordinary reading never needs the writer's permission.
For a scarce test device or other exclusive resource, use the repository's actual
scheduler. A useful handoff names its request, resource and terminal/running state;
messages do not allocate the resource or hold the Git window. Notify a dependent
owner when the result changes their next action, not for every progress step.

## Natural boundaries and complete content

Handle returned action digests at phase, resume or commit boundaries. Use `sync`
when reaching a natural boundary without a digest. Read selected full messages,
requests and readiness receipts when previews omit required content. Follow
returned offsets/cursors to completion for the selected record; do not load all
workspace history for routine coding.

Message previews include sender, subject, thread, body size and an explicit
`summary_excerpt` when incomplete. Treat excerpts as discovery, not a complete
instruction. For several selected IDs use `message-batch --ids ...` instead of
one call per message. Continue with the same IDs and returned `next_index`; read
remaining bodies through `message ID --offset NEXT_OFFSET`. Metadata and attachment
indexes retain their own continuations. Batch reads never present, consume or
acknowledge messages; use `consume-batch` only after handling them. Consume the
selected IDs after acting, including a message whose requested action is already
settled by inspected evidence. Keep an unresolved action pending; a read or
preview alone is not handling. Answer tracked decisions separately.

For attachments, page the index with `attachments`, then join all `reference`
chunks returned by `attachment` before parsing JSON. Evidence references remain
data. After acting, `consume`/`consume-batch` record message handling, `ack`
records a requested receipt, `request-resolve` records a decision and
`dependency accept` records reviewed readiness. None substitutes for another.
Avoid acknowledgment conversations, polling, heartbeats and per-command hooks.

## Producer and consumer handoffs

A consumer can `dependency subscribe` to a known producer and artifact, specifying
the paths and next action. The producer prepares the full counterpart scope,
performs locally required checks and publishes `ready` with useful evidence.
Use `handoff` when publishing readiness and sending the recipient notification
together. For a materially shared producer change that affects a known consumer,
provide one settlement handoff: changed contract, complete counterparts, intended
invariant, source/check receipt and remaining consumer work. Use the readiness
notification instead of duplicating it in several messages or activity records.
No handoff is required for ordinary independent edits. Do not claim verification
from a source reading or a queued operation.

The consumer reads `dependency updates` and `readiness`, checks complete paths
and current hashes, examines evidence and accepts the current update before
dependent integration. Unrelated commits do not invalidate unchanged inputs.
Changed inputs or producer generation can invalidate a receipt; refresh the
handoff rather than accepting stale readiness. Withdraw obsolete receipts and
cancel obsolete subscriptions explicitly.

## Commit ownership

Review the settled diff and run the checks required by the repository. If a
local commit is authorized, select exact fully owned files with `commit execute
--paths ... --message ...`. This uses a private index and preserves unrelated
staging. File/folder activities are not commit reservations.

For a mixed file, capture the full base commit, prepare and review an owned-hunk
patch, then use `--patch-file` and that `--base-commit` with the exact paths. The
CLI computes the patch hash; inline patches require the exact SHA-256. Do not
adopt peer staging, commit a whole mixed file or revert a whole mixed commit.
The service refuses overlapping staged hunks; resolve the ownership conflict
instead of dropping another actor's work. An optional `--bump-version` follows
the repository's configured rule; it is not a default imposed by Agentcoord.
After a confirmed rejection because a peer committed any selected path, including
disjoint hunks, inspect the new HEAD and rebuild and review the owned patch
against its full commit ID before retrying.

Native execution reserves selected paths for preparation and briefly serializes
the final Git update. Use `commit acquire` only for necessary manual shared-index
operations, wait for the exact granted receipt and release that exact grant.
Cancel only a pending owned admission. Run tests outside this window; commits
never submit or await quality checks.

If publication is uncertain, retain the operation ID and retry key. Inspect
`operation get`, `receipt` and `commit reconcile` before retrying. A published
commit remains published even if later notification or cleanup fails. Recovery
must discover the existing outcome, not create another commit.

## Missing native binding

For `UNBOUND_ACTOR`, inspect the first failure once. Correct a confirmed missing
lifecycle/trust/setup cause before retrying; a different environment spelling or
invented session ID cannot provide proof. If both tools and CLI lack supported
native context, continue independent work and the safe Git outage procedure
below. Report the limitation once. No heartbeat, inbox or diagnosis loop.

## Coordination outages

An unavailable adapter, daemon or peer is not a prerequisite failure for ordinary
coding, checks or an authorized commit. If only the MCP adapter is unavailable,
use the installed CLI. If the service is unreachable, stop coordination calls
after the first diagnosis; retry only after a concrete relevant change. Continue
independent work from current source, diffs and already available evidence. Keep
a short local task receipt of any important unsent outcome; publish one summary
at the next natural boundary after connectivity returns, rather than replaying
every missed activity or sending repeated acknowledgments.

For fully owned files with reviewed changes and required checks complete, use
Git directly during an outage:

```sh
git commit --only -m 'Describe the owned change' -- path/to/owned-file
```

Select every intended owned path explicitly. Newly created files must first be
added with `git add -- exact/new/paths`; tracked files need no preliminary staging.
Git's normal locks protect the index and ref update, and `--only` preserves
unrelated staged work. Honor local version policy and include only an owned
version change. Keep existing hooks enabled; do not clear lock files, reset
staging, stage whole folders or bypass genuine ownership conflicts. A post-commit
notification failure does not undo publication: inspect Git's commit and selected
diff before deciding whether anything remains to commit.

Do not switch to direct Git when a native commit is already queued/running or
its publication is uncertain. Reconcile that operation's retained receipt and
actual Git outcome first. An explicit refusal for conflicting paths or authority
is not an outage. Mixed owned/peer hunks require their reviewed patch workflow;
defer only that overlapping slice when safe commit ownership cannot be established.
Missing peer replies block only work that truly needs their decision, never the
rest of the task. No new watcher, restart loop or terminal-input messaging is
part of outage recovery.

## Native identity, checkpoints and scheduling

`identity` uses actual harness session and process-start evidence. Reconnects
preserve actor/task identity, while task, connection and execution generations
remain distinct. A shared MCP connection cannot impersonate a child. Independent
children need their own native binding; `delegate` only consents to supported
scoped parent routing. Do not invent IDs or choose identity from pane labels,
transcripts or display names.

For delayed native operations, capture the actual `identity` result and pass
global `--origin-context` with exactly `workspace_id`, `actor_id`,
`task_generation` and `execution_generation` canonical UUIDs, with a non-null
execution. This snapshot restricts the current native binding; it does not
replace it. Preserve the original guard across retries instead of refreshing it
to acquire new authority.

Use `checkpoint` at a meaningful pause and `complete` when work is done. A
checkpoint's resume preference is an explicit choice. Do not enable
`--resume-enabled` or schedule a native resume without user opt-in. An authorized
reminder and an authorized resume are different jobs. `schedule --kind resume`
requires a supported target and verified offline execution with unchanged
authority; paused, completed, live or ambiguous targets are not restarted.
Messages do not start an offline harness. Live sessions use automatic native
attention; offline scheduled resume has separate consent.

Inspect exact jobs with `job`/`jobs`. An uncertain external launch needs deliberate
`resolve`/operation reconciliation before optional retry. Elapsed time or a lease
does not justify launching it again. Cancellation signals only owned launched
process groups; it never terminates arbitrary user sessions.

## Setup, lifecycle hooks and activation

For an authorized installation task, preview `init` and inspect its changes;
`init --apply` registers the exact workspace and installs the marked guidance,
shared skill, optional project configuration and selected harnesses' project
MCP/lifecycle configuration. `--harnesses` selects from Claude, Codex, Cursor and
Grok, with all selected by default. Existing user rules retain authority and
unrelated tools/hooks are preserved. User-modified installer resources are
refused rather than overwritten. Claude/Cursor native skill pointers route to
the same `.agents` authority used directly by Codex/Grok; CLAUDE.md imports
AGENTS.md. Equivalent existing native handlers may be reused to avoid duplicate
events. `init --candidate-dir PATH` emits inert configuration, instruction and
skill candidates when review without activation is needed. Use one
service per registered workspace: foreground `serve` on Linux/macOS or managed
launchd on macOS. Private state remains outside the checkout.

Hooks call `agentcoord hook HARNESS EVENT` with the native JSON payload. Supported
events are `start`, `stop`, `failure`, `end`, `child_start` and `child_stop`; these
are observations from lifecycle boundaries, not prompts or per-command pings.
Payloads need an absolute originating `cwd`, `workspace_root` or `project_dir` in
the selected workspace. A configured service route does not prove origin, and a
separately registered nested workspace belongs to its own service. Missing,
invalid or foreign paths are ignored before native connection. Events without
originating execution correlation cannot complete work, release protected
execution or authorize resume.

Review complete changed hook definitions and approve them through the harness's
native trust mechanism. Confirm actual client loading and execution, then use
`doctor --live` to distinguish configured, connected, bound and observed states.
Parsed config and reachable service alone do not prove all open clients reloaded
their integration. Unsupported approval mechanisms require operator review;
the installer never bypasses trust. Hooks and MCP catalogs can remain cached
until a natural session reload. Refresh clients before retiring old commands.
Cached MCP sessions use the installed CLI with explicit
`--project` until a natural reconnect refreshes their catalog. Never call retired
cached tools, inject terminal input or force-restart sessions to refresh them.

## Maintenance and retry recovery

A normal restart retains work. Explicit `service drain` fences new mutations and
external starts, allows inspection and waits for owned effects before maintenance.
Managed upgrade/removal must confirm quiescence. Unknown health, foreign units
and uncertain effects fail explicitly; do not bypass them by killing sessions.

`backup`, `restore` and `migrate` are offline under the same exclusive ownership
lock. Restore refuses to overwrite retained records. Review a migration manifest,
retain the import run ID across retries, verify all mapped records/relationships
and explicitly activate only after verification. Service removal retains history.
After a backup-only removal, use `service install --apply`, then `service activate`
and `doctor --live`; installation alone does not clear the durable drain fence.

For any uncertain mutation, retain the actor-scoped retry key and exact payload.
Use `receipt`/`operation get`; retrying the same key and payload recovers the
receipt, while changing it is rejected. Durable acceptance does not guarantee
message presentation, handling or native execution. `operation ack` acknowledges
a failed version without retrying it; domain reconciliation resolves uncertainty.
Operator monitor/snapshot/history reads do not consume agent actions.

The local OS user is the trust boundary. Private sockets and native bindings
prevent accidental session/workspace mixing; they do not sandbox malicious code
running as that same user.

## Wake delivery activation

Senders use ordinary `send` for every harness. Native attention is automatic by
default; no wake flag, recipient status lookup or per-agent handshake is needed.
`--no-wake` / MCP `wake: false` keeps a selected message quiet. Repository
`[native] wake_enabled = false` disables delivery; explicit `wake configure`
preferences are current-execution only. Paused/offline sessions do not restart.
Completed tasks can receive messages in their live session without reopening
task authority. Wake and scheduled offline resume are separate features.

Codex must use its existing shared app-server daemon with the target thread
loaded in this repository. Claude's Agentcoord MCP advertises a Channel; launch
interactive Claude with `--dangerously-load-development-channels server:agentcoord`
and complete native trust prompts. That flag does not approve tool use and cannot
activate a running client retroactively. Research-preview access/policy applies.
Grok currently requires a verified live shared-leader ACP owner; existing
standalone TUIs have no supported attach endpoint. Cursor has ordinary messaging.

An incoming native signal asks for one `sync`. Read relevant data through tools,
preserve task authority and intentional fixes, act, then consume handled messages.
Do not reply with an acknowledgment unless an actual decision needs an answer.
Continue independent work if native delivery is unavailable or uncertain; inspect
an exact receipt when needed, never poll or blindly resend.
