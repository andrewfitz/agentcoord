# Agentcoord command reference

Run in the checkout or use global `--project /absolute/repository` (also accepted
after the subcommand). Installed help owns exact syntax. CLI names below map to
the same underscore-named MCP operations unless stated CLI-only.

## Identity and reported work

- `identity`: native binding, attribution limits and available capabilities.
- `activity --task NAME --paths FOLDER --note TEXT`: one meaningful scope.
  A native owner can begin newly named work after a completed task atomically.
- `activity --state completed --evidence TEXT`: inherit recorded scope/note.
  Different actual output paths can be described in evidence.
- `activities`, `evidence`: retrieve relevant current work and intentional fixes.
- `intent`: preserve one non-obvious settled fix during longer work; not every edit.
- `checkpoint`, `complete`: explicit lifecycle/task boundaries. Pause and
  scheduled resume consent are separate from starting new authorized work.
- `delegate`: supported scoped routing, never child impersonation.

## Messages and boundary actions

```sh
agentcoord send --recipients UUID --kind handoff --subject 'Parser ready' --body 'Escapes preserved; focused tests pass.'
agentcoord send --recipients UUID --kind reply --subject 'Parser result' --body 'Integrated.' --reply-to MESSAGE_UUID
agentcoord sync
agentcoord consume-batch --ids HANDLED_UUID ANOTHER_HANDLED_UUID
```

Ordinary directed sends request native attention automatically; no recipient
presence check is needed. Add `--no-wake` only to deliberately quiet a message.
Replies retain a server-verified original link and default conversation/scope;
recipients remain explicit and context grants no authority.

`sync` presents a bounded digest, default three actions, actor maximum twenty.
Complete short message bodies need no extra fetch. An incomplete preview is not
the whole instruction: use `message ID` or `message-batch --ids ID ...` and follow
`next_offset` / `next_index` / cursors. `inbox` peeks; reads never mark handling.
`consume` / `consume-batch` mark acted-on messages; `ack` is only a requested
receipt. `history` and `search` retain imported history and old conversations.
Attachment indexes and bodies have their own continuations.

## Dependency decisions

`question --recipient UUID --subject TEXT --body TEXT --paths PATH` creates or
reuses the exact current tracked question in one call. `request-find` /
`request` remain available for deliberate discovery and explicit creation.
Use `request-get`, `request-resolve`, `request-defer`, `request-follow` and
`request-transfer` for their stated decision transitions. An answer, requester
cancellation or authorized transfer is explicit; silence and elapsed time grant
nothing. Continue independent work.

## Artifact readiness

`dependency wait --producer UUID --artifact NAME --paths FILE --next-action TEXT`
atomically registers a subscription and checks existing readiness, then returns.
It never blocks the model. Inspect any returned receipt and actual counterpart
paths/hashes before acceptance. `dependency subscribe`, `dependency updates`,
`readiness`, `ready` and `handoff` use the same records.
Withdraw obsolete receipts and cancel obsolete subscriptions explicitly.

## Shared-checkout commits

```sh
agentcoord commit execute --paths src/parser.py --message 'Fix escape parsing'
agentcoord commit execute --paths src/parser.py --patch-file /absolute/scratch/owned.patch --base-commit FULL_REVIEWED_BASE --message 'Fix escape parsing'
```

Capture the full base before preparing the reviewed patch. The CLI snapshots and
hashes external patch inputs; changed Git paths stay restricted to the checkout.
Small UTF-8 snapshots use bounded inline submission; use explicit `--local` for
larger/non-UTF8 snapshots. `--bump-version` follows configured repository policy;
`--adopt-staged` is deliberate and never sweeps peer staging.

`commit status` observes admission; `commit acquire` / `commit release` cover
manual shared-index work with exact grants. `commit cancel` cancels a pending
request. `commit reconcile` inspects a recorded uncertain effect without
publishing another commit. Retain retry keys and inspect publication outcomes.

## Runner resources

CLI-only `resource status --provider NAME [--request-id ID]` reads a configured runner's JSON
status. `resource event --provider NAME --event-file FILE --recipients UUID`
forwards an explicit wait/admission/terminal event through ordinary authenticated
messaging with a stable event key. The runner allocates resources and owns
deadlines/cancellation; absent fields are unknown, not guessed. There is no
second scheduler or automatic watcher. Host adapters can notify from their
existing result invocation.

Configure `[resources.providers.NAME]` with `argv` and optional
`timeout_seconds` (1..30, default 10). An optional request ID is appended as one
literal non-option argument. JSON status contains `request_id`, `resource`,
`state`, `holder`, `queue_position`, `start_deadline` and `result`; unsupported
fields use null. States are queued/running or passed/failed/cancelled/expired/
error/superseded. Only terminal states carry a result object. An event file is
`{"event_id":"stable-id","kind":"terminal","status":{...}}`; kinds wait,
admitted and terminal must match the status. Output is bounded to 64 KiB and
individual observations to 6 KiB.

## Scheduled work and durable effects

Use `schedule`, `jobs`, `job`, `cancel` and `resolve` only for an authorized
delayed reminder/resume or exact retained effect. Resume requires explicit user
opt-in and supported verified offline native authority. A queued operation is
not completion. `operation`, `operation-list` and `receipt` inspect actual
effects; a stable key binds the original arguments/generations.

## Installation, lifecycle and operator maintenance

`init` previews integration; `init --apply` writes managed resources while
preserving unrelated settings. `doctor --live` separates configuration, service,
binding and observed Channel registration. `service upgrade` previews the
managed service transition; `service upgrade --apply` applies it.

`monitor`, operator `snapshot` and `history` are read-only. Current views omit
diagnostic-only children; all/history retains them. Carryover counts preserve
earlier assignments and archived recipients; they are not automatic decisions.
Backup/restore/migration and explicit service maintenance are exceptional
operator actions; consult installed help and the relevant workflow.

## Native attention

Use ordinary send. `wake get` inspects exact attention outcomes;
`wake reconcile` resolves only supported evidence and sends nothing.
`wake configure` is a current-execution preference, not offline resume consent.
