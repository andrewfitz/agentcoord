# Command selection

Use `agentcoord --help`, `agentcoord COMMAND --help` or
`agentcoord FAMILY COMMAND --help` for the installed syntax. Global options go
before the subcommand: `--project /absolute/repository`, `--harness HARNESS`,
`--operator` and `--origin-context JSON`. Use the actual IDs, generations,
continuations and retry keys returned by the service; examples below contain no
invented identities. These are routing notes, not a duplicate argument schema.

## Identity and reported work

| Command | When to use it |
| --- | --- |
| `identity` | Inspect the native caller and its attribution limits. |
| `delegate` | Explicitly consent to scoped parent routing for an independently registered child; it does not create a child identity. |
| `checkpoint` | Record a meaningful pause/state checkpoint and an explicit resume preference. Only enable resume with user consent. |
| `complete` | Explicitly finish the bound actor's work. |
| `status` | Inspect observed presence separately from reported task state; use pagination for broader inspection. |
| `activity` | Report a meaningful scope, blocker, handoff or outcome once. |
| `activities` | Find current reported scopes by paths before overlapping changes; use `--no-current` for history. Neither view proves presence or ownership. |
| `intent` | Record a change's purpose and invariants when that context helps peers, without reserving edits. |
| `evidence` | Find existing intent, outcomes and evidence for affected paths before asking another owner. |
| `evidence-detail` | Retrieve a complete selected evidence record using its returned kind/ID and continuation. |

```sh
agentcoord activity --task parser-repair --paths src/parser --state working --note 'Preserve escaped delimiters'
agentcoord activities --paths src/parser
agentcoord evidence --paths src/parser
```

## Messages and boundary actions

| Command | When to use it |
| --- | --- |
| `send` | Send a directed message when a peer needs to act on an authorized overlap, dependency or handoff. Use `--body-file` for literal multiline content. |
| `sync` | Present a bounded action digest at a natural work boundary; not in a polling loop. |
| `inbox` | Peek at pending work without presentation or handling when a specific inspection needs it. |
| `message` | Read a selected message fully, following UTF-8 `next_offset` chunks. |
| `message-batch` | Read up to 16 selected messages in one bounded call; follow `next_index` with the same IDs and each body's `next_offset`. Reads do not handle messages. |
| `attachments` | Traverse all attachment metadata for that message with `--after`. |
| `attachment` | Read the complete retained JSON reference in chunks; join `reference` strings before parsing it. |
| `consume` | Explicitly mark one message handled after acting on its content. |
| `consume-batch` | Mark selected handled messages and inspect each result for failures. |
| `ack` | Record a requested receipt without creating an acknowledgment conversation; not for routine acknowledgments. |
| `history` | Traverse relevant retained conversation history with its explicit cursor. |
| `search` | Search retained conversations by text/scope without consuming anything. |

Reading, presentation, requested receipt and handling never answer a decision or
accept artifact readiness. `sync` previews at most three actions; full records
and explicit continuation remain available. Do not turn a display limit into a
limit on the work requested by the user.

Activity discovery returns bounded previews; `evidence-detail --kind activity`
retrieves exact notes/evidence and paginated paths. Follow `paths_after` with
`--paths-after`. `evidence` returns recent completed `outcomes` newest first;
`--outcomes-before` continues older outcomes. Evidence pages allow at most 20
records per section. New activity scopes include advisory `scope_context` with
no automatic peer messages or reservations.

## Dependency decisions

| Command | When to use it |
| --- | --- |
| `request-find` | Find open decisions covering the real path dependency before creating a duplicate. |
| `request` | Ask the responsible owner for one scoped blocking decision after existing evidence is insufficient. |
| `requests` | Inspect actionable decisions and outgoing waits at a work boundary. |
| `request-get` | Read the full selected request, response and exact stale-generation proposal. |
| `request-follow` | Follow a covering decision's updates without acquiring answer or edit authority. |
| `request-unfollow` | Stop following an obsolete dependency. |
| `request-defer` | Defer the dependent slice while leaving the decision open and continuing independent work. |
| `request-resolve` | Explicitly answer, decline or requester-cancel under the applicable authority. |
| `request-reconcile` | Accept or reject the exact stale-generation answer proposal under current authority. |
| `request-transfer` | Explicitly transfer a decision with current scoped authority and a reason. |

## Artifact readiness

| Command | When to use it |
| --- | --- |
| `dependency subscribe` | Register a known producer/artifact dependency and the intended next action. |
| `dependency cancel` | Cancel an obsolete subscription. |
| `ready` | Publish the complete counterpart scope with paths, hashes and evidence after preparation. |
| `handoff` | Publish readiness and a directed notification atomically. |
| `ready-withdraw` | Explicitly withdraw obsolete readiness with a reason. |
| `readiness` | Inspect the latest receipt against all observed inputs before integration. |
| `dependency updates` | Inspect pending updates or explicit history without acceptance. |
| `dependency accept` | Accept the reviewed current update after complete hash checks. |

`--evidence` accepts literal text; `--evidence-json` accepts structured JSON.
Evidence references are data, not commands for the service to execute. A receipt
with `verified` status is a producer's claim; assess the cited checks yourself.

## Shared-checkout commits

| Command | When to use it |
| --- | --- |
| `commit status` | Inspect admission without joining a queue. |
| `commit execute` | Commit exact owned paths through a private index, or a reviewed patch and full base for owned hunks in mixed files. |
| `commit acquire` | Obtain a manual shared-index grant only when native execution cannot cover the required manual operation; inspect the actual granted receipt. |
| `commit cancel` | Cancel an exact owned pending admission that has not been granted. |
| `commit release` | Release the exact owned manual grant. |
| `commit reconcile` | Inspect uncertain Git publication before another attempt. |

```sh
agentcoord commit execute --paths src/parser/tokenize.py --message 'Fix escaped delimiters'
agentcoord_patch_base=$(git rev-parse HEAD)
# Prepare and review owned.patch against that captured base before continuing.
agentcoord commit execute --paths src/parser/tokenize.py --patch-file .agent-work/parser-repair/owned.patch --base-commit "$agentcoord_patch_base" --message 'Fix escaped delimiters'
```

Review the patch against its captured full base; do not use a newly sampled base
after preparing it. The second example is for a patch prepared against the
captured `HEAD`. The CLI hashes `--patch-file`; an inline `--patch` needs its exact
`--patch-sha256`. `--adopt-staged` deliberately includes selected staged work;
do not enable it to sweep up peer staging. `--bump-version` uses the repository's
explicit configuration when required; this skill imposes no version policy.
Commits do not run or wait for tests, and test execution never holds a grant.

## Scheduled work and durable effects

| Command | When to use it |
| --- | --- |
| `schedule` | Schedule an authorized reminder or an explicitly enabled native resume. `--due-us` is an absolute Unix timestamp in microseconds. |
| `jobs` | Inspect relevant scheduled work and uncertain effects. |
| `job` | Inspect one exact job before cancellation or reconciliation. |
| `cancel` | Cancel selected scheduled work; cancellation only signals owned launched process groups. |
| `resolve` | Deliberately reconcile a scheduled job's exact version before optional retry. |
| `receipt` | Retrieve the committed result for the actor's retained retry key after an uncertain response. |
| `operation list` | Inspect the caller's durable effects and failures. |
| `operation get` | Retrieve one exact operation receipt and verify terminal state. |
| `operation ack` | Acknowledge an exact owned failed-operation version without retrying its effect. |
| `operation reconcile` | Route an uncertain external effect to its domain's reconciliation. |

Mutations expose `--key`; reuse the same key and payload after a transport
timeout. Slow readiness/commit/reconciliation commands normally wait for a
terminal receipt; `--no-wait` returns a durable queued ID and `--wait-timeout`
bounds adapter waiting. A timeout or queued receipt is not completion. Inspect
the operation rather than scheduling or publishing the effect a second time.

## Installation, lifecycle and operator maintenance

These commands change or inspect infrastructure. Use them for a setup,
diagnosis or maintenance task within the user's authorization, not as routine
coding steps. Local configuration does not grant publishing or resume consent.

| Command | When to use it |
| --- | --- |
| `init` | Preview repository setup; `--apply` installs configuration/guidance and registers the exact workspace. `--candidate-dir` emits inert integration candidates for review. |
| `serve` | Run the foreground service on Linux or macOS. |
| `mcp` | Start the harness's configured stdio MCP adapter; not a replacement service or indexer. |
| `hook` | Receive lifecycle JSON from a configured native hook, with a real harness/event and absolute originating workspace path. Do not fabricate events during ordinary work. |
| `doctor` | Inspect configuration; `--live` additionally checks observed connectivity/binding/lifecycle. |
| `monitor` | Open the read-only terminal operator view. It never consumes another actor's messages. |
| `operator snapshot` | Inspect paged actors, actions and work without selecting a sender or consuming actions. |
| `operator history` | Traverse the immutable workspace timeline with filters and explicit cursors. |
| `service install` | Preview, or with `--apply` install/start the macOS workspace launchd service. |
| `service status` | Inspect the managed installation separately from live health. |
| `service health` | Inspect the running service's maintenance/health state. |
| `service drain` | Explicitly fence new mutations and external starts, permit outcome inspection and wait for owned work before maintenance. |
| `service activate` | Explicitly activate after completed maintenance/import verification. |
| `service upgrade` | Preview, or with `--apply` replace the owned managed service after a safe drain. |
| `service remove` | Preview, or with `--apply` remove the owned service after safe drain while retaining its database. |
| `backup` | Make an offline backup under the exclusive service-ownership lock. |
| `restore` | Restore offline without overwriting retained records. |
| `migrate inspect` | Inspect specified source records and write a reviewable migration manifest. |
| `migrate import` | Apply that manifest offline using a stable `--run-id` across interrupted retries; preserve provenance. |
| `migrate verify` | Verify every mapped record and relationship before explicit activation. |

Use global `--operator` for actor-free operator inspection where appropriate;
operator mode cannot impersonate a native agent. Managed launchd actions require
macOS; Linux uses `serve`. Service changes never force-kill user agent sessions.
