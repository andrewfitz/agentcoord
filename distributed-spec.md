# Agentcoord specification: local improvements and distributed coordination

Status: Draft for implementation. Date: 2026-10-05.
Baseline: Agentcoord 0.1.5, package implementation commit
`348cae610673c6e071ddc5c996a42610c77713d8`.

This specifies proposed behavior. Network operations and commands below are new;
they are not available in the installed release. This document does not authorize
implementation, deployment, credential issuance or migration of existing data.
The current local protocol remains in [protocol.md](protocol.md).

Delivery has two stages: improve one repository on one machine first, then add
multiple repositories and distributed workers. Stage one is independently useful
and ships before stage two. The detailed scope and exit gates are in
[section 16](#16-verification-and-delivery-sequence). Network leases, encryption
requirements and source exchange below belong to stage two unless stated otherwise.

## 1. Outcome and boundaries

Support many coding agents sharing one checkout on each participating machine,
with several machines and cloud workers editing the same repositories. Execution
and source checkouts remain distributed. There is no worktree per agent and no
requirement that all coding happens on the hub.

Provide repository-isolated messages, decisions, meaningful work discovery,
short file leases, recoverable publication and safe incorporation of peer changes.
Make interconnected work discoverable without command announcements, perpetual
LLM polling, acknowledgment chains or broad edit freezes.

Stage-two boundaries:

- One coordination hub deployment, serving multiple repositories.
- One shared checkout per machine and repository, with multiple local agents.
- Existing local daemon and harness integration own local execution and Git.
- Existing Git remotes carry source; the hub carries coordination metadata.
- Authenticated repository access; no workspace selected by an untrusted path.
- HTTPS and encrypted coordination databases/backups in distributed mode.
- Local coding, checks and authorized commits continue during hub outages.
- Bounded operations, durable receipts and explicit uncertainty.

Not included: blockchain, peer-to-peer consensus, federation, distributed
filesystems, code CRDTs, remote shells, cloud-agent provisioning, a new test
scheduler, per-agent branches/worktrees, per-command interception, or automatic
model wakeups from message text. No Redis, external queue or general account UI.

## 2. Deployment and authorities

| Component | Responsibility | Explicitly not responsible for |
| --- | --- | --- |
| Hub | Access grants; repository actor/task state; conversations; decisions; leases; accepted publication manifest; repository event stream | Local process verification, shell execution, editing checkouts, running tests |
| Worker daemon | Native harness binding; local workspace mapping; local commit/index protection; lease renewal; publication preparation; incorporation journal | Creating repository access grants or claiming remote process proof |
| Git remote | Transfer immutable source objects through existing Git authentication | Message access control or distributed lease ownership |
| CLI/MCP/monitor | Bounded projections and typed operations through these authorities | A second source of state or arbitrary sender selection |

A local `workspace_id` identifies one checkout. A hub `repository_id` identifies
one collaboration space shared by those checkouts. A `machine_id` identifies an
enrolled installation; a random worker instance ID distinguishes concurrent or
restarted daemons. None is an access credential.

The operator explicitly maps a local workspace to a hub repository. Equal folder
names, paths or remote URLs never establish membership. One checkout cannot be
connected to two publishing hubs. A separate experimental checkout must be
explicitly registered and cannot impersonate the main worker.

Hub repository domains are authoritative for network conversations, task
assignments, leases and publication acceptance. Worker storage owns native
execution, local Git operations and application receipts. Received hub records
are bounded caches, not independently writable copies of hub state. Disconnected
notes are local drafts, not falsely accepted messages or lease decisions.

Standalone local mode remains usable. Connecting a populated local workspace
requires an explicit migration of its coordination authority; do not run local
and hub decision authorities in parallel. Execution/Git receipts remain local.

Repository bootstrap is explicit. An operator-qualified worker derives a complete
path/mode/SHA-256 inventory from a named full Git commit, never from an unreviewed
dirty worktree. Import the inventory in bounded pages and finalize its expected
count/digest, Git object format and baseline commit ID before activating the
repository. Missing paths mean absent paths, not unknown content that the first
publisher may claim. Other workers fetch and verify the same baseline. Unsupported
source kinds may exist in that baseline but cannot be selected for publication.

Normal product history and accepted source publications are separate. Version one
does not automatically push or merge product branches. The repository's integration
owner handles those Git operations explicitly; this does not serialize independent
local commits. Committing already accepted bytes does not republish them. Source
changes introduced through external Git pulls must be reconciled and published
under current leases before replacing hub path heads; a branch update cannot
silently reset the accepted source authority.

## 3. Technology and reuse

Use Python and the existing typed operation catalog, validators, chunking,
retry-key semantics, findings/evidence distinctions and installed skill.
Transport adapters must not reimplement those domain rules.

Use Starlette/Uvicorn for a small HTTP boundary and HTTPX for pooled clients.
Use SQLCipher through a maintained SQLite-compatible Python driver. Pin and test
its release dependencies before shipping; require binary packages for supported
release targets. Do not introduce a custom cipher or silently fall back to
plaintext if the driver/key is unavailable.

Use Git plumbing and transport for source artifacts. Caddy may terminate public
HTTPS; deployment through Tailscale or another private network is supported.
A private network supplements authentication rather than replacing it.

No distributed lock library is required: one hub transaction owns leases and
accepted revisions. Start with one hub process; horizontal replicas are outside
this release. A restarted hub uses the same durable authority and recovery rules.

## 4. Repository isolation and access

Each repository has its own encrypted coordination database. A small encrypted
hub catalog maps repository IDs to database/key references and access-grant
metadata. No endpoint searches all repository databases for an agent request.

The operator issues separate grants for a repository and enrolled principal.
A principal may be an individual agent or a machine broker authorized to bind
its local native sessions in that repository. Revoke principals independently;
never use one shared repository password for every machine.

Capabilities are `read`, `coordinate` and `publish`:

- `read`: permitted repository work discovery and message projections.
- `coordinate`: authenticated messages, activity, decisions and lease sessions.
- `publish`: source publication, in addition to the relevant coordination grant.

Repository access does not make every message public. Preserve existing
sender/recipient visibility. A separate explicit repository-observer grant can
read its conversation history; neither agents nor query flags can enable it.
`publish` does not grant a Git remote credential, shell access or administrator
rights. Administration initially uses a local operator CLI on the hub, not an
agent-facing API.

Credentials contain at least 256 random bits, use opaque token values and have
independent expiry/revocation. Persist only verifiers, using a cryptographic hash
of high-entropy tokens and constant-time comparison. Tokens go in the HTTPS
Authorization header, never URLs, logs, message content, Git or committed config.

A worker broker enrolls using its repository grant and receives actor-scoped
connection capabilities for native sessions. The broker keeps grant secrets
outside agent tool output; tool calls cannot choose sender identity. Actor IDs
are namespaced by repository, principal, machine and native session/child tuple.
An authenticated principal may register only actors within its own grant policy.
An individual-agent grant cannot enroll siblings or create machine-wide rights.

Bind repository, actor and capability before dispatch. Resolve all IDs inside
that repository database. Validate every recipient, attachment, decision,
subscription, cursor and retry receipt there. Inaccessible resources return a
uniform unavailable result without revealing another repository's existence.
Client-supplied `operator`, machine IDs, labels and paths do not expand authority.

Authorization precedes dispatch for every operation, including retries and event
reads. Negotiate protocol/schema compatibility at connection; refuse unsupported
mutations explicitly rather than translating them through a legacy fallback.

Revocation immediately blocks new mutations and renewals. Accepted receipts
remain retained. Existing leases become unusable under that revoked grant;
expired/revoked owners cannot publish under captured tokens. An authenticated
read still requires a currently valid grant, including after lease revocation.

Machines do not authenticate remote process liveness. The worker reports native
observations with machine, execution generation and observation age. The hub
labels them as reported observations. Only the originating worker can verify or
act on its OS processes. Shared OS users remain a trust boundary: credential
brokering does not sandbox mutually hostile processes owned by that user.

## 5. Network protocol

One endpoint, conceptually `POST /v1/repos/{repository_id}/operations`, dispatches
a bounded typed operation after authentication. It accepts protocol version,
operation name, arguments, request ID, retry key and applicable generation guards.
The server constructs caller context; it does not accept caller-selected authority.

The hub exposes an explicit catalog of network-safe repository operations.
Local commit execution, hook configuration, process signals, native resume and
operator administration are never reachable by changing an operation name.
An agent message is never executable input to these local capabilities.

Initial limits:

| Boundary | Default |
| --- | --- |
| Request/response body | 256 KiB, after decoding; explicit continuations |
| Message body | Existing 64 KiB UTF-8 limit |
| Recipients / selected message read batch | Existing maximum 16 |
| Natural-boundary action digest | Existing 3 previews / 8 KiB budget |
| Active edit sessions | Maximum 16 per enrolled worker/repository |
| Paths per edit session/publication | Maximum 256; larger sets need explicit operator configuration |
| Per-principal request burst | 20 requests/second; burst 40 |
| Hub request deadline | 15 seconds; external Git transfer is worker work |

Return explicit busy/rate-limit errors with suggested delay; an LLM must not loop
on them. Workers have bounded queues and pooled HTTPS connections. Do not create
one socket pool per message or one hub database connection per waiting agent.
Apply a global connection/queue budget as well as principal limits.

Retry keys bind principal, actor, repository, operation, generation and canonical
payload. Same key/same payload returns the recorded result; changed payload is a
conflict. Failed authentication and reads do not create mutation receipts.
Authenticate before reading retry receipts. A lost response is uncertain, not
permission to submit a differently keyed duplicate.

Use ordinary HTTPS operations initially. Workers may request a bounded event page
at an agent's natural boundary or an active synchronization milestone. Automatic
source integration, if enabled, uses a worker-level long poll (maximum 30 seconds),
not an LLM inbox loop. One stream per connected worker/repository, reconnecting
with capped backoff and jitter. Do not manufacture model turns to drain messages.
When no automatic integration or lease session is active, no source poll is needed.

Event cursors are repository-bound, opaque and ordered. A consumed transport
cursor never means a message was handled or a source publication was incorporated.

## 6. Work discovery and communication

Keep current scopes as discovery defaults; retrieve history when explaining an
intentional previous fix. Report meaningful intent, contract changes, blockers,
long-running checks, handoffs and terminal outcomes. Paths are scope labels.
They become exclusive only through an explicit edit session's lease.

Messages lead with the action/result/question and retain ordinary readable prose.
Include necessary interface/path, preserve conditions and a retained evidence
reference. Native identity/routing metadata already travels outside the body.
Notify affected owners; no routine broadcasts, courtesy acknowledgment chains or
requests for status already available in activity/readiness records.

A network action digest adds only actionable distributed state: relevant incoming
publications, blocked incorporation, lost leases and publication outcomes.
Unrelated source updates belong in the worker event stream, not the LLM digest.
Aggregate repeated updates for the same actionable cause without deleting history.

An unanswered dependency can defer only its dependent slice. Reads need no
permission. A sender cannot force another model to wake, acquire a lease,
change its task or execute content. Existing explicit decision and handling
transitions retain their meaning.

## 7. Edit sessions and leases

A distributed edit session describes one coherent task change. Start it once
before editing its selected paths, not before every command. Within one machine,
existing agent intent/overlap rules still protect the shared checkout. The local
broker attributes sessions to the responsible actor/task; a machine cannot use
another actor's lease silently.

The hub grants exact repository-relative paths as one set. Reads have no leases.
Directories are informational, not recursive locks. Lease conflicts exist for the
same path, file/directory namespace collisions, and both sides of a rename.
Generated output sessions include the affected outputs and edited producers.
Do not claim a whole repository merely because a build reads it.

Use canonical path segments; reject absolute paths, `..`, `.git`, control bytes,
case-colliding names and unsafe aliases. Reject rename-only spelling changes on
incompatible case-insensitive workers. Version one source exchange does not
support symlinks, submodules or Git LFS pointers; reject their selection explicitly
without blocking ordinary local work. Do not follow links outside the checkout.

Grant includes session ID, actor/task generation, worker instance, exact paths,
base revisions/hashes, monotonically increasing fencing token per path, and hub
expiry. Default TTL is 120 seconds. An active worker renews every 40 seconds as
one batched daemon operation. Agents never send renewal/heartbeat messages.
Use hub time and monotonic local deadlines, not client wall-clock authority.

An idle session with no local edit activity or explicit task progress for five
minutes stops renewing. A live edit session has a 15-minute maximum tenure:
publish a coherent milestone or explicitly renew task ownership before that
limit, allowing a contending actor's request to become visible. User pauses,
completion, worker shutdown and task-generation changes stop renewal. No process
is killed because a lease is lost. Do not hold leases through long tests/audits;
release publication scope and bind later evidence to its tested inputs.

Acquisition is immediate, all-or-nothing, and returns owner/session/scope context
on conflict only within repository visibility. No durable blind wait queue.
The actor continues independent work or sends one specific handoff request.
Extending a session is also atomic. A rejected extension never partially acquires
its new paths; if the task cannot advance, checkpoint/release its existing set
rather than holding it while waiting for another set. Partial explicit release
invalidates that path's authority for any later publication from this session.

After acquiring, incorporate the granted paths' accepted base versions before
editing. Existing local dirty content is preserved as a draft/overlap, not erased.
Offline edits can remain local, but obtaining a later lease does not validate their
old base: they require reconciliation against current accepted content.

Locks constrain hub acceptance, not arbitrary filesystem writers. Cooperative
agents use edit sessions and local write coordination. Human/unmanaged writes
are detected at snapshot/incorporation boundaries, retained and surfaced. Do not
claim enforced edit prevention without a separately authorized OS sandbox.

## 8. Source exchange through Git

Use an operator-configured Git remote and a separate carrier ref per machine,
under `refs/heads/agentcoord/checkpoints/{machine_id}`. These are transport refs,
not task branches or worktrees. The worker never force-pushes, checks out,
merges or cherry-picks a carrier ref into the product branch.

A checkpoint stores sparse Git trees containing the selected paths only:

1. A base snapshot commit records the selected preimage tree, with the previous
   carrier commit as parent, or no parent for the first checkpoint.
2. A result snapshot commit records the selected postimage tree, with the base
   snapshot commit as parent. Its ID is the checkpoint ID.
3. Push the carrier ref by normal fast-forward Git transport. The previous ref
   must match the recorded expected value; an unexpected remote update needs
   reconciliation, never force replacement.

Absent entries express creation/deletion. File modes are explicit. This preserves
reachability of both snapshots and older checkpoints while needing one remote
ref per worker. A checkpoint is a content carrier, not a normal product commit.
Its parent/tree structure is validated before use. Sparse content is never
presented as a complete repository snapshot or executable checkout.

Build trees with a private temporary index and captured selected bytes. Never
stage the user's index, capture all dirty files, invoke project commit hooks,
bump a version, or claim normal commit/test success for a checkpoint. Normal
`commit execute` retains its existing checks/hooks and receipt semantics.

Capture stable pre/post content with hashes under cooperative local write
coordination. If bytes change while capturing, retain the operation as needing
fresh capture; do not publish a mixed snapshot. New files, deletes, executable
mode changes, rename endpoints and unintended untracked files need explicit scope.

Every selected change must be task-owned or explicitly adopted from its responsible
peer. A distributed lease does not confer ownership of local peer hunks. Retain
incorporated publication provenance for normal commit review; neither a checkpoint
nor a clean hash match establishes authorship of unrelated local work.

The manifest binds publication ID, repository, machine/actor/task, session,
path fencing tokens, checkpoint/base commit IDs, per-path before/after SHA-256
and modes, purpose, preserve conditions, and dependencies/evidence references.
Use both Git object IDs and independently computed SHA-256 content hashes. A
rename is an explicit delete/create pair; no heuristic grants edit authority.

Default source limits are 8 MiB per selected file and 32 MiB of combined preimage
and postimage bytes per publication. Operator-approved larger limits must match
the receiving workers. Bound decoded content, object inspection, temporary storage
and fetch deadlines, not just compressed transfer size. The complete bootstrap
repository is outside this selected-blob limit and has its own operator budget.

Git URL, executable and credential provider come from operator configuration.
Messages cannot supply fetch URLs, executable paths, command options or credential
helpers. Transport uses argv arrays and validated refs, not interpolated shell.
Agents need normal Git access in addition to hub publication permission.

The hub accepts manifests, not source payloads, and does not execute or fetch code.
Acceptance proves scoped authority/revision agreement, not source correctness.
Receiving workers fetch the exact named objects from the configured remote,
verify the snapshot structure, complete diff, modes and SHA-256 manifest, then
record verified availability. Undeclared changes are never applied. An invalid
carrier becomes an explicit rejected artifact and blocks only its dependent slice;
an operator marks its artifact unusable and authorizes a corrective publication
to restore usable heads. Retain the original immutable acceptance receipt and
the corrective linkage; never erase history or reset fencing counters. Recovery
uses verified source and explicitly reviewed current heads, not a timestamp win.
A grant holder can still author harmful in-scope code; source
review/testing and execution isolation remain necessary.

Source bytes and Git history follow Git-remote access and retention, not message
visibility. Never imply that database encryption encrypts the Git remote or local
checkout. Use the Git provider's access controls and encrypted host volumes where
needed. Avoid placing credentials or private logs in checkpoints.

## 9. Publication acceptance and recovery

Worker states are `captured`, `uploaded`, `accepted`, `rejected`, `uncertain`.
Persist the publication ID and manifest hash before any external effect. Use the
same ID/key throughout. The local journal records checkpoint objects, expected
carrier ref, completed transfer and hub receipt.

The hub transaction validates grant, actor/task, session, current fencing tokens,
lease expiry, every path's before hash/revision, modes and bounded manifest.
Dependencies required for acceptance name exact accepted repository publications.
Require access to each referenced repository; unauthorized references leak no
metadata. Cross-repository changes are explicitly linked, not a distributed
transaction: a consumer waits for the declared set to be accepted/verified locally.

One successful transaction records the immutable manifest, advances all selected
path heads under one repository publication sequence, records its retry receipt,
and releases those selected leases. Other session paths remain held until
explicit release or expiry. Acceptance and release are atomic in the hub database.
Disjoint publications may share the same earlier global sequence if their
selected path bases still match; do not require every machine to catch up with
unrelated files before publishing.

Git upload and hub acceptance are not atomic. Upload first; accept only while
leases remain current. A crash after upload leaves a discoverable unaccepted
carrier checkpoint. A crash after hub acceptance leaves a discoverable accepted
receipt. Reconcile both using IDs/ref/object hashes before another attempt.
Uploaded/rejected content never advances accepted heads. Expired-owner content
stays a draft requiring current-base reconciliation; it is not auto-resubmitted.

No transport failure rolls back a real local Git commit or invents a successful
hub publication. Store publication receipts independently from product commits.

The sending worker records local incorporation only after rechecking that its
selected working bytes/modes still equal the accepted postimages. Acceptance
alone cannot prove this: a local agent may already have started a further edit.
Retain such edits without labeling them as the accepted version.

## 10. Incorporation into a dirty shared checkout

Start in preview/manual mode. Automatic clean incorporation is an operator
opt-in per workspace, not an effect of receiving a message or installing MCP.
In either mode, only authenticated, verified manifests are candidates.

Track last incorporated publication/hash per path. The accepted head, downloaded
availability and actual local content are separate states. A worker does not
claim it has incorporated a publication because it read its message.

For a coherent publication group:

1. Fetch and verify both sparse snapshots and manifest. Refuse missing objects,
   wrong paths/modes, unsupported entries and size violations.
2. Acquire the worker's short local write-coordination window for selected paths;
   do not acquire another distributed lease just to incorporate accepted source.
   An active local edit session on those paths defers incorporation.
3. Capture actual working-file and selected index state. Unrelated files/index
   entries are untouched. Dirty selected staging or untracked collisions defer
   the group without cleanup.
4. If every selected path matches its preimage, stage the new bytes in private
   temporary files. If every path already matches its postimage, reconcile as an
   idempotent completed application. A recognized partial prior application uses
   its existing journal; arbitrary mixed state requires review.
5. Journal the complete before/after set before replacements. Recheck content
   immediately before writing and use safe root-relative descriptor traversal
   and atomic per-file replacement. Never resolve links outside the workspace.
6. Verify all postimages/modes and record completion, then emit one useful outcome.

Filesystem replacement across multiple files is not atomic. During the short
apply window, cooperative writers defer those paths. Readers need no permission,
but test/build evidence spanning a source update cannot be called stable.
Recovery recognizes only journaled preimages/postimages. If unexpected peer bytes
appear, stop and retain them. No blanket rollback, stash, reset or restore.

A base mismatch is a pending integration issue, not permission to overwrite.
Version one does not automatically three-way merge dirty files. An agent can
prepare/review a focused three-way merge using Git, acquire the relevant edit
session, qualify its current bases, and publish the resolution. No conflict
markers are written into a live shared file by the synchronizer.

If a worker missed several updates, traverse bounded publication history in
accepted order for relevant paths. Keep complete group dependencies. A checkpoint
is never incorporated solely because its newer timestamp or sequence wins.

## 11. Interconnected code, readiness and tests

File leases prevent competing accepted writes to the same paths. They do not
prevent semantic conflict between different files. Publish complete edited
producer/consumer/generated counterparts where practical; describe interface
changes and preserve conditions in the readiness handoff.

Consumers subscribe to known artifacts and inspect exact receipts rather than
asking repeatedly whether an owner is done. Readiness binds repository, accepted
publication(s), exact paths/hashes, producer generation and evidence.
Unrelated publications do not invalidate unchanged inputs. Missing counterpart
publications or changed declared inputs defer only the dependent integration.

Testing remains local or in the existing scheduler. Record machine, request/run
ID, actual outcome and tested source/input identity. Hashes captured before and
after are useful change detectors but do not prove an intermediate read was
stable. Use the repository's existing immutable test-input mechanism when that
proof matters; do not add per-agent worktrees or whole-repo locks to every test.
If relevant source moved and execution inputs cannot be established, the result
is qualified evidence, not proof of the current tree. Release write leases before
long verification; evidence can be added to the settled publication later.

Exclusive test devices belong to the existing scheduler and its machine/resource
namespace. Source lease expiry never terminates a test or frees a scheduler-owned
resource. A commit does not start, restart or await tests.

## 12. Offline, shutdown and uncertain work

| Condition | Behavior |
| --- | --- |
| Hub unreachable | Stop agent-level retries; keep independent local edits/tests/commits working. Worker reconnects with bounded backoff. |
| Lease renewal uncertain | Retain captured tokens; query exact session on reconnect. Conservatively stop shared publication when validity is unknown. |
| Lease expired/revoked | Keep local edits; acquire current authority and reconcile bases before publication. |
| Git transfer uncertain | Inspect exact remote carrier ref and immutable object before uploading again. |
| Hub acceptance uncertain | Inspect publication/retry receipt; never manufacture another publication ID. |
| Incorporation interrupted | Recover the existing journal against exact bytes; preserve unexpected writes. |
| Agent paused/completed | Stop its edit-session renewal; preserve drafts and outstanding decision records. |
| Worker restarted | Obtain a new instance identity; recover drafts/receipts. Do not inherit old leases without an explicit current-authority transaction. |
| Hub restarted | Preserve access policy and receipts; invalidate all outstanding leases before new grants. Existing accepted publications remain accepted. |

For safe hub restart, advance a durable hub epoch and expire outstanding leases
before serving mutations. Fencing counters never reset. This avoids interpreting
old deadlines through a changed server clock. Workers observe an epoch change as
lost leases; retained drafts remain useful. Renewal scheduling uses monotonic
elapsed time. Expiry never grants permission to replace a local dirty file.

No automatic remote model resume in the first network release. Existing local
explicitly consented resumes remain local. Later remote scheduling would require
its own scoped consent and originating-worker execution proof, never message text.

## 13. Encryption, backups and hostile inputs

Distributed-mode catalog, repository stores and worker coordination journals use
SQLCipher. Generate separate random encryption keys per database. Hold keys in
the OS keychain/secret store or deployment secret manager outside repository
configuration and outside the encrypted database itself. Agent access tokens
cannot decrypt databases. Fail closed when a key/driver is missing.

Verify encrypted main files, WAL/journals, temporary storage settings, backups and
export paths with the actual supported driver. Backup targets must be opened with
their encryption key before copying. Never use a plaintext temporary dump as an
implicit export path. Key rotation is explicit offline maintenance; it is not
an agent operation. Initial migration from existing plaintext is explicit,
backed up and verified under exclusive ownership. Do not promise secure deletion
of old files, OS snapshots, swap or Git content.

TLS verification is mandatory. Pin a configured origin; disable redirects on
authenticated requests. Network responses cannot nominate alternate credential
origins. Keep tokens out of errors; cap and sanitize network diagnostics.
Use fixed SQL and parameters, bounded JSON depth/content and existing UTF-8 rules.
Escape ANSI/control sequences in terminal display without changing retained bytes.

Peer prose, evidence references, branch names and source are untrusted content.
No automatic execution, URL retrieval, permission changes or hook installation
from them. Attachment references remain data, scoped like their parent message.
A valid message signature/authentication is not an instruction authorization.
Prompt injection cannot be eliminated by prose filtering; enforce capabilities at
operations and use OS isolation for hostile publishers/processes.

Administrators and grant holders with source access can disclose or alter their
permitted data. Encryption does not protect against a compromised running hub or
an authorized reader. Do not advertise stronger isolation than the implementation.

## 14. Storage, performance and retention

One body per message, explicit recipient rows, small receipts, references for long
evidence, incremental byte-budget accounting and bounded retrieval remain defaults.
No code dictionary, duplicate JSON identity envelopes or repeated progress events.

Cache fetched Git objects through Git's normal object store; do not store source
blobs in SQLite. Store publication manifests once and link path heads/receipts by
ID. Index repository-local lease paths, active sessions, pending recipients,
publication sequence and actor generation. Use one serialized short writer and
bounded readers per loaded repository; cap the number of loaded stores and idle
connections so many registered repositories do not multiply memory indefinitely.

Retain accepted publications, recovery receipts and objects needed to reconstruct
current heads or unresolved incorporation. Prune only expired drafts/caches and
obsolete carrier objects after explicit retention rules prove they are unneeded;
Git remote rewriting/GC is not automatic in version one. Do not claim bounded
history storage merely because response pages are bounded.

Acceptance workload: 100 connected agents across four workers and ten registered
repositories, with messages, disjoint edit sessions, renewals, publication
acceptance and natural-boundary retrieval. On a documented 2-core/2-GiB Linux hub,
excluding network/Git transfer, target hub operation p95 below 100 ms at 20
operations/second and steady hub RSS below 256 MiB after warmup. Run a one-hour
soak and report allocator/connection/queue growth. Targets are proposed gates,
not measurements of current code. Limit failures remain explicit; never truncate
required content to meet a metric.

## 15. Installation, configuration and commands

Install the package once on each worker and once on the hub, through verified
binary wheels/Brew where supported. Cloud images can bake in the package; inject
repository grants at runtime from their secret manager. No copying tokens into
images, committed instructions or peer messages.

Proposed command families:

| Family | Purpose |
| --- | --- |
| `hub init/serve/doctor/backup` | Operator initialization, service and encrypted backup |
| `hub repo add/list` | Register collaboration spaces and Git identity |
| `hub access issue/revoke/list` | Repository principals/capabilities; secrets output only on explicit issuance |
| `connect --repo ... --hub ...` | Map local workspace; enroll through secret input/keychain, never a token argument in shell history |
| `edit begin/extend/status/release` | One coherent distributed edit session |
| `publish prepare/send/status/reconcile` | Capture, transfer and accept selected changes with one durable ID |
| `updates list/inspect/apply/reconcile` | Preview and safely incorporate received source |
| Existing message/activity/readiness/commit commands | Reuse current protocol, routed to their correct authority |

Names are proposed contracts; settle parser schemas and MCP catalog together.
Hub administration is CLI-only. Network-mode MCP exposes edit/publication
operations but does not expose grant secrets, hub administration or remote exec.
Existing local `commit execute` remains the owned-file commit mechanism.

Committed workspace config contains hub URL, repository ID, configured Git remote,
and synchronization policy. Machine-specific local mapping holds machine ID,
workspace ID and secret references. Secrets and local paths stay outside Git.
Automatic clean incorporation is off until explicitly enabled by the operator.

Installer updates shared skills and all selected harness MCP/lifecycle pointers.
Instructions explain when to start an edit session, handle overlap/lost leases,
publish checkpoints, incorporate updates, qualify evidence and continue offline.
Hooks remain lifecycle-only. Existing native trust/reconnect requirements remain.

For operators, monitor repository/machine/actor work, accepted versus locally
incorporated revisions, pending decisions, edit sessions, lost leases and source
application failures. Every view stays repository-authorized and read-only.
A human repository observer cannot silently acquire an actor's identity.

Migrating existing conversation history preserves record IDs/provenance, recipient
visibility and unresolved uncertainty. Imported sessions are historical records,
not recreated live actors. Retire the old local messaging authority only after
verified hub cutover. A failed migration keeps the old authority active and
distributed mode inactive; never expose two writable conversation authorities.

## 16. Verification and delivery sequence

### Stage one: single-repository improvements

One machine, one shared checkout, many agents. Keep the existing local daemon,
SQLite authority, CLI/MCP, monitor and native harness bindings. Reuse what already
works; these are deliverable outcomes, not a request to rebuild existing features.

| Area | Deliverable |
| --- | --- |
| Work discovery | Compact current task scopes, relevant owners and long-running checks; clear separation between reported work, observed execution and historical sessions. |
| Intentional edits | Retrieve the latest relevant outcome by path/scope, including why a fix exists, preserve conditions and exact evidence when available. Reuse activity/readiness records; extend their structured references only where missing. No per-edit journal or parallel Markdown ledger. |
| Overlap handling | Detect meaningful scope overlap and direct one actionable handoff to the affected owner. Folder scopes stay advisory; exact-path reservations apply to real conflicting work or short Git operations, never routine reads or every command. |
| Dependencies | Reuse tracked decisions and readiness receipts; avoid duplicate questions and status requests. Only the dependent slice waits; unrelated work continues. |
| Message delivery | One bounded actionable digest at natural boundaries, with explicit detail retrieval and handling. Coalesce repeated causes in previews without deleting distinct questions or evidence. No chat injection, forced model turns, broadcasts or inbox loops. |
| Lifecycle and recovery | Keep retired sessions historical, bound active connections and recover uncertain effects by existing IDs. Missing native proof cannot establish liveness or release another agent's authority. Maintenance belongs in the existing daemon, not additional watchers. |
| Commits and outages | Preserve private-index owned-file/hunk commits and brief final Git serialization. Independent preparation proceeds concurrently. Service failure cannot veto safe local work; uncertainty about a prior commit still requires reconciliation. |
| Storage and performance | Indexed bounded projections, one copy of message bodies, small linked evidence, bounded queues/connections and safe cleanup of disposable caches. Pending decisions, unhandled messages and recovery evidence are not disposable. |
| Installation and agent instructions | Idempotent harness setup, lifecycle-only hooks, a concise daily SOP and commands for overlap, handoff, intentional fixes, tests and outages. Verify binary-only release installation; no surprise native compilation. |
| Operator view | Show current work, actionable overlaps, decisions, test evidence and historical/uncertain presence clearly. Inspection never impersonates an agent or changes its state. |

Folder overlap alone never sends a message or blocks work. Surface relevant
context on a meaningful scope change; ask an owner only when the actual shared
file/interface or intended behavior requires a decision. Intentional-fix records
describe purpose and evidence, not permanent immunity from later justified fixes.

Implement only missing capabilities or confirmed defects after comparing these
outcomes with the installed baseline and real usage. Keep shared typed handlers
behind local transports; avoid network placeholders and duplicate domain services.
No hub, TLS, grants, SQLCipher dependency or Git source synchronization is required
to use or release stage one. There is no mandatory distributed edit session in
this stage and no universal write gate.

Stage-one exit gates:

- Demonstrate Claude and Codex exchanging and handling a real directed question
  through tools in the same repository, with correct native attribution.
- Demonstrate an intentional fix discoverable by the next agent, a genuine overlap
  resolved without reverting peer work, and disjoint work continuing independently.
- Demonstrate independent local commits preserving peer staging; tests never hold
  the commit window. Service outage does not block a reviewed safe commit.
- Exercise restart, stale-session cleanup and uncertain commit recovery without
  duplicate effects or invented liveness. Pending work survives cleanup.
- Review actual harness transcripts for command/read ceremony, duplicate messages,
  unnecessary waiting and missing actionable handoffs; fix confirmed causes.
- Benchmark 100 connected native actors in one workspace with bounded projections
  and mixed activity/message/decision traffic. Report p50/p95 latency, RSS, queue
  growth and database growth over one hour on a documented machine. Use the
  section 14 latency/memory targets as proposed local gates; keep local results
  separate from later network measurements.
- Verify a clean install/upgrade and the affected regression tests, then release
  the local package and updated instructions as a usable independent milestone.

### Stage two: multiple repositories and network coordination

Build on the released stage-one domain services. Keep standalone local mode
usable; only explicitly connected repositories switch their conversation and
distributed publication authority to the hub. Add multiple-repository isolation
and network support together, with repository-specific grants from the start.

Deliver the following dependent slices inside this stage; do not ship source
synchronization before its safety boundaries exist.

1. Repository IDs, principal grants, isolated databases and network-safe catalog.
   Reuse current message/decision/readiness domains and typed transports. Verify
   denial across every ID/cursor/attachment/retry path and grant revocation.
2. SQLCipher driver/key management, HTTPS and encrypted backup/recovery. Verify
   wrong/missing keys, plaintext scans, TLS failures, quotas and secret redaction.
   No network mode ships with a plaintext fallback.
3. Machine/native actor binding, broker capability storage and installed skills.
   Test real supported harness boundaries; mark unsupported adapters explicitly.
4. Edit sessions, fencing/base revisions, expiry/epoch restart and atomic sets.
   Test contention, scope extension, revocation, old-worker publication and offline
   draft preservation. Different files must proceed independently.
5. Sparse Git checkpoint capture/transfer and publication state machine. Use
   actual temporary Git repositories and a Git remote. Test exact path scope,
   shared-index preservation, failed/uncertain push, rejected expired publication,
   acceptance timeout and unchanged normal commit-hook behavior.
6. Preview/incorporation journal and optional clean application. Test dirty files,
   peer staging, untracked collisions, case/mode/delete conflicts, hostile paths,
   interrupts at every replacement and retained unexpected peer edits.
7. Cross-machine readiness, monitoring, installer updates and performance soak.
   Demonstrate two real worker environments plus a cloud worker sharing a repo;
   multiple native agents per worker; and an agent denied another repository.

Stage-two end-to-end acceptance:

- Two workers concurrently publish disjoint changes without a repository freeze.
- Two workers request the same file; only one gets current publication authority.
- A stopped/expired old owner cannot overwrite a newer accepted publication.
- A dirty receiver retains local work and defers only the affected update group.
- A message cannot cause execution, expand grants or leak another repository.
- A source artifact whose actual diff contradicts its manifest is never applied.
- Lost responses/crashes reconcile existing Git/hub outcomes without duplicates.
- Local commits/checks continue during hub failure without claiming remote success.
- Multiple related files reach verified local completion after interrupted apply,
  or remain explicitly unresolved with every unexpected peer byte preserved.
- Encrypted backups restore under the correct identity/key/schema; wrong keys,
  retained destination state and incompatible protocols fail explicitly.
- Independent subagents stay correctly attributed; shared-parent connections do
  not invent child identities. Lifecycle observation never becomes remote proof.

Audit the complete authorization/publication/incorporation flow once after its
implementation settles, repair validated defects and run affected checks. Tests,
platform acceptance, source review and performance measurements remain separate
claims. A spec, configured adapter or clean textual merge is not runtime proof.

## 17. Settled design choices and limits

Central coordination, distributed execution. One shared checkout per worker,
no worktree per agent. Existing Git carries sparse change artifacts; one carrier
ref per worker. Exact-file leases, current-base checks and fencing protect accepted
publication. Dirty source merging is reviewed, not automatic. Local tests and
commits never require hub permission merely because messaging is unavailable.

The system promises repository-scoped authenticated operations, recoverable
publication and preservation of unexpected local edits under its cooperative
protocol. It does not promise zero divergence, semantic conflict elimination,
atomic multi-file filesystem replacement, trustless consensus or protection from
arbitrary privileged processes. Those limits stay visible in instructions and
operator views rather than being hidden behind successful-looking defaults.
