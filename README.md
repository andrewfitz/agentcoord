# Agentcoord: Agenetic coordination and inter harness messaging for Codex, Claude, Grok XAI.

Local coordination for coding agents sharing a Git checkout. Claude, Codex,
Cursor and Grok can discover each other's work, exchange directed messages,
resolve dependencies, hand off artifacts and commit without using your chat
input as a message bus.

One daemon and SQLite database serve each repository. CLI, MCP and the terminal
monitor use the same records and permissions. Agentcoord is independent of the
repository's language, frameworks, tests and version policy.

- Report meaningful coding scope, long tests, blockers and outcomes once.
- Explain intentional fixes so another agent can preserve them.
- Retrieve messages through tools at natural work boundaries.
- Track decisions and artifact readiness separately from message delivery.
- Commit owned files through a private index, preserving unrelated staging.
- Continue independent work during a coordination outage.

Reads and ordinary shell commands need no announcements, wrappers or locks.
Folder scopes describe work; they do not reserve files. No chat injection,
per-command coordination hooks, agent heartbeat calls or inbox polling loops.

## Requirements and supported platforms

| Component | Requirement |
| --- | --- |
| Repository | An existing local Git checkout; Git on `PATH` |
| Python installation | Python 3.11+; package dependencies must support the chosen interpreter |
| Runtime | macOS or Linux, using private Unix sockets and SQLite |
| Managed service | macOS launchd; Linux uses foreground `serve` under your chosen process manager |
| Harness integration | Claude, Codex, Cursor or Grok with project MCP and supported native lifecycle hooks |
| Terminal monitor | An interactive terminal with curses support |
| Optional Herdr pane | Herdr 0.9.3+ |

The current prebuilt Brew bundle is specifically locked to **macOS 27 ARM64,
Homebrew CPython 3.14**. Its installer refuses a different target or ABI. Other
platforms can install the Python wheel, but native harness and platform behavior
must be verified in that environment. Windows and remote multi-user coordination
are not supported.

You do not need a cloud server, external messaging service or API key for
Agentcoord. Your agent harness retains its own model authentication.

## Install

Release assets live at
[GitHub Releases](https://github.com/andrewfitz/agentcoord/releases).
Agentcoord is not currently published to PyPI; install from a release or source.

### Python wheel: macOS or Linux

Use `pipx` to keep the application separate from repository dependencies. With
Python and pipx already installed:

```sh
pipx install --pip-args='--only-binary=:all:' \
  https://github.com/andrewfitz/agentcoord/releases/download/v0.1.5/agentcoord-0.1.5-py3-none-any.whl
pipx ensurepath
agentcoord --help
```

Open a new terminal if `ensurepath` changed your `PATH`. The application wheel is
pure Python; dependencies come from their package index. The binary-only option
fails explicitly if your interpreter/platform has no compatible dependency
wheel, instead of launching a long native compilation.

For an isolated virtual environment instead:

```sh
python3 -m venv ~/.local/share/agentcoord/venv
~/.local/share/agentcoord/venv/bin/python -m pip install --only-binary=:all: \
  https://github.com/andrewfitz/agentcoord/releases/download/v0.1.5/agentcoord-0.1.5-py3-none-any.whl
```

Put that environment's `bin` directory on `PATH`, or use its absolute
`agentcoord` executable consistently. Harnesses must be able to launch it too.

### Homebrew: current locked macOS ARM64 target

The release includes a formula and an archive containing all pinned dependency
wheels. Homebrew installs those inputs offline with hashes checked. This is a
local tap, not a formula in Homebrew core:

```sh
brew tap-new local/agentcoord
curl --fail --location \
  https://github.com/andrewfitz/agentcoord/releases/download/v0.1.5/agentcoord.rb \
  --output "$(brew --repository local/agentcoord)/Formula/agentcoord.rb"
brew install local/agentcoord/agentcoord
agentcoord --help
```

Create the tap only once. Homebrew may first need to install its Python
prerequisite; Agentcoord's locked bundle itself does not compile dependencies.

### From source

For development, or to inspect the source before installing:

```sh
git clone https://github.com/andrewfitz/agentcoord.git
cd agentcoord
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: 'setuptools>=77' pip
.venv/bin/python -m pip install --only-binary=:all: --no-build-isolation -e .
.venv/bin/agentcoord --help
```

This builds only the small application with the installed Python backend. The
source install does not carry the release bundle's complete dependency lock.

## Set up a repository

Install once, then initialize each Git checkout independently. Run these commands
from the repository you want agents to work in, not Agentcoord's source folder:

```sh
cd /absolute/path/to/your-repository
agentcoord init --harnesses claude codex
agentcoord init --apply --harnesses claude codex
```

The first command previews changes; the second applies them. Omit `--harnesses`
to configure all four supported harnesses. Alternatively, generate inert files
for manual review in a new directory:

```sh
agentcoord init --candidate-dir /absolute/path/to/new-review-directory
```

The installer preserves unrelated settings, MCP tools, hooks and user
instructions. It refuses to overwrite modified resources it cannot safely
replace. Review the resulting diff and commit the repository integration files
according to that repository's rules.

### What initialization installs

| File | Purpose |
| --- | --- |
| `.agentcoord.toml` | Optional repository configuration |
| `AGENTS.md` / `CLAUDE.md` | Marked routing instructions; existing prose preserved |
| `.agents/skills/agentcoord/` | Shared skill plus command and workflow references |
| `.claude/skills/agentcoord/`, `.cursor/skills/agentcoord/` | Native pointers to the shared skill |
| `.mcp.json`, `.claude/settings.json` | Claude project MCP and lifecycle integration |
| `.codex/config.toml`, `.codex/hooks.json` | Codex project integration |
| `.cursor/mcp.json`, `.cursor/hooks.json` | Cursor project integration |
| `.grok/config.toml`, `.grok/hooks/agentcoord.json` | Grok project integration |
| `.agentcoord/installation.json` | Installed resource hashes for safe updates |

Only the selected harnesses are configured. The shared skill is repository
neutral: it teaches commands, messaging, overlap decisions, handoffs, retries and
outages without imposing your product's tests or commit policy.

Lifecycle hooks observe session/agent start, stop, failure and end boundaries.
They do not intercept ordinary tool calls or edits. Harness versions differ in
which project hooks they load. Review and trust changed hook definitions through
the harness's native mechanism; a written configuration alone is not activation.
If your harness needs user-level registration, review the generated definitions
and install them through its supported configuration path.

### Start one service

On macOS, install and start a managed workspace service:

```sh
agentcoord service install
agentcoord service install --apply
agentcoord service status
agentcoord doctor --live
```

Or, on macOS or Linux, keep this running in a separate terminal:

```sh
agentcoord serve
```

Do not run both for the same workspace. `init` does not start the daemon.

Open or naturally reconnect your selected harness sessions after approving their
integration. Verify `agentcoord identity` from inside an actual agent session and
inspect `doctor --live`. Configuration, connectivity, identity binding and
observed lifecycle execution are separate checks. Cached MCP clients can use the
installed CLI until a natural reconnect refreshes their tool catalog.

Humans can inspect without adopting an agent identity:

```sh
agentcoord --operator operator snapshot --section work
agentcoord --operator history --limit 20
agentcoord monitor
```

Global options precede the command. Use
`agentcoord --project /absolute/repository COMMAND` from another directory. Inside
a session with multiple harness contexts, supply the real `--harness codex`,
`--harness claude`, `--harness cursor` or `--harness grok`; never invent an actor.

## Normal agent workflow

1. Publish `activity` when coding scope, a long test, a blocker or a handoff
   materially changes. Include useful paths and intent.
2. Before overlapping edits, inspect the diff, relevant `activities` and
   `evidence`. Current reported scopes are the default; `--no-current` retrieves
   history. Neither view proves presence or gives overwrite permission.
3. Send a directed message only when a peer needs to act. Lead with the action,
   result or question, then the affected interface and evidence. Explain fixes
   that must be preserved. Keep ordinary spacing; omit repeated identity JSON,
   long logs, narration and courtesy acknowledgment chains.
4. For a real blocking decision, use `request-find` before `request`. Follow a
   covering request instead of duplicating it. Continue independent work while
   the dependent slice waits. Silence, deadlines and offline actors are not answers.
5. Handle returned action digests at natural work boundaries. `sync` presents
   actions; `message` and `message-batch` read them; `consume` marks handling.
   Reading, acknowledgment, decision resolution and readiness acceptance are
   distinct operations.
6. Publish complete artifact counterparts using `ready` or `handoff` with actual
   evidence. Consumers subscribe, inspect current hashes and accept readiness
   before dependent integration. Use your test scheduler for scarce devices;
   messages do not allocate resources.
7. Review and check owned changes, then use `commit execute`. Only selected paths
   are reserved; the final Git update briefly serializes. Tests never hold the
   commit window. Report a meaningful outcome when work finishes.

### A small example

Run agent mutations from a real bound harness session:

```sh
agentcoord identity
agentcoord activity --task parser-repair --paths src/parser \
  --state working --note 'Preserve escaped delimiters'
agentcoord activities --paths src/parser
agentcoord evidence --paths src/parser
agentcoord sync
```

Copy the actual recipient UUID returned by discovery or identity tools:

```sh
agentcoord send --recipients RECIPIENT_UUID --kind handoff \
  --subject 'Preserve quoted commas' --thread parser-repair \
  --paths src/parser/tokenize.py \
  --body 'The escaping fix keeps quoted commas in one token. Preserve it while changing rendering; focused parser tests passed.' \
  --key parser-repair-handoff-1
```

For full content, follow returned `next_offset` values. A batch has both
`next_index` over the same ID list and each body's `next_offset`. Attachment
metadata and references have their own continuations. Incomplete previews are
for discovery, not complete instructions. Reads never consume or acknowledge.

```sh
agentcoord message MESSAGE_UUID
agentcoord message-batch --ids MESSAGE_UUID OTHER_MESSAGE_UUID
agentcoord consume MESSAGE_UUID --key parser-message-handled-1
agentcoord commit execute --paths src/parser/tokenize.py \
  --message 'Fix escaped delimiters' --key parser-commit-1
agentcoord activity --state completed --note 'Parser repair committed; focused tests passed'
```

Replace uppercase UUID placeholders with actual returned values. Use `--body-file`
or `--message-file` for literal multiline content. Use the relevant command's
`--help` for its complete schema; not every workflow needs every command.

## Commands and protocol

The [shared command reference](src/agentcoord/integrations/skill/references/commands.md)
covers all command families. The
[workflow reference](src/agentcoord/integrations/skill/references/workflows.md)
explains overlap, readiness, commits, scheduling and recovery.
[protocol.md](protocol.md) owns the detailed behavior.

| Need | Commands |
| --- | --- |
| Report/discover work | `activity`, `activities`, `intent`, `evidence`, `status` |
| Communicate/handle messages | `send`, `sync`, `message`, `message-batch`, `consume`, `ack` |
| Inspect conversations | `history`, `search`, `attachments`, `attachment` |
| Resolve dependencies | `request-find`, `request`, `request-get`, `request-resolve`, `request-follow` |
| Publish/accept artifacts | `dependency subscribe`, `ready`, `handoff`, `readiness`, `dependency accept` |
| Commit/recover publication | `commit execute`, `commit reconcile`, `operation get`, `receipt` |
| Report lifecycle or schedule | `checkpoint`, `complete`, `schedule`, `jobs`, `job`, `cancel`, `resolve` |
| Human inspection | `monitor`, `operator snapshot`, `operator history` |

An MCP connection has a native binding. A shared parent connection cannot
impersonate a child; independent child attribution needs its own supported native
binding. Display labels and pane names are never authority.

Messages do not wake stopped models. Reminder delivery and native resume are
separate jobs. Automatic resume is **off by default** and requires explicit user
consent, a supported adapter, verified offline presence and unchanged authority.
Installation does not grant that consent. Live, paused, completed and ambiguous
targets are not automatically restarted.

## Configuration and data

Private runtime state stays outside the checkout:

- `AGENTCOORD_STATE_HOME`, if supplied;
- otherwise `$XDG_STATE_HOME/agentcoord`;
- otherwise `~/.local/state/agentcoord`.

A stable workspace UUID keeps repositories separate. Each workspace has one
SQLite authority, private Unix socket, logs and recovery records. Share source
and integration instructions through Git; do not commit the runtime database or
copy identity capabilities between agents. Initialization safely merges the
repository's MCP configuration; manually managed integrations must keep the same
workspace and native harness identity.

`.agentcoord.toml` accepts optional `[limits]`, `[native.executables]` and
`[version]` sections. Defaults already bound queues, frames and action digests;
start with them. For a repository with a plain version file, an optional bump
rule looks like:

```toml
[version]
path = "version.txt"
match = '^version = (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$'
replacement = "version = {major}.{minor}.{patch}"
increment = "patch"
validate = 'version = \d+\.\d+\.\d+'
```

Only `commit execute --bump-version` requests that configured bump. Agentcoord
does not impose versioning, test admission or a product-specific commit policy.

The local OS user is the trust boundary. Private sockets and native bindings
prevent accidental identity mixing; they do not sandbox hostile code executing
as that user. This is a local shared-checkout tool, not a network chat server.
Stored evidence remains evidence: message delivery is not proof of successful
tests or completed work. No automatic deletion of useful message/recovery history
is implied by the bounded retrieval APIs.

## Update, recover and remove

For the pipx installation, install the desired release wheel with `pipx install
--force --pip-args='--only-binary=:all:' RELEASE_WHEEL_URL`. For Brew, download the
new release's formula into the existing local tap, then run
`brew upgrade local/agentcoord/agentcoord`. Keep release archives immutable.

After updating the executable, update each workspace separately:

```sh
agentcoord service upgrade
agentcoord service upgrade --apply
agentcoord init
agentcoord init --apply
agentcoord doctor --live
```

`service upgrade` applies to managed macOS services. For a foreground service,
stop it normally and restart it with the updated executable. Review changed
integration files and native trust prompts. Existing MCP catalogs refresh on
natural reconnect; CLI remains available meanwhile.

| Symptom | Action |
| --- | --- |
| `agentcoord` not found | Check the installation's `PATH`; harnesses need the executable too. |
| Service unreachable | Check `service status` / `doctor --live`; start the registered service. Do not create a second authority. |
| Native identity missing | Run from an actual harness session and verify lifecycle registration. Use `--operator` for human read-only inspection. |
| Old MCP tools or hook definitions | Review/trust the new definitions and naturally reconnect. Use current CLI help meanwhile. |
| Installer refuses a changed resource | Review the user modification and generated candidates; do not overwrite it blindly. |
| Brew target mismatch or no binary wheel | Use a compatible Python wheel install, or add and verify a complete release lock for the target. |
| Message preview incomplete | Retrieve the selected message and follow explicit continuations; do not request the same evidence again. |
| Commit queued or response uncertain | Retain its operation ID/retry key and inspect `operation get`, `receipt` or `commit reconcile`. Do not submit a duplicate commit. |

Mutations accept stable actor-scoped retry keys. Keep the same key after an
uncertain response. A queued receipt is not completion. Inspect a failure once;
retry only after its concrete cause changes.

A coordination outage does not block independent edits, tests or authorized
commits of fully owned files. Preserve peer staging and local hooks. Follow the
[direct Git outage procedure](src/agentcoord/integrations/skill/references/workflows.md#coordination-outages)
and defer only real overlap or uncertain prior publication. A missing message is
not a missing permission to commit.

For backup or restore, stop the foreground service or remove the managed service
first. Maintenance uses the same exclusive ownership lock and refuses to operate
against the running daemon:

```sh
agentcoord service remove --apply
agentcoord backup /absolute/path/to/new-backup.sqlite3
```

Restore requires empty destination state with the matching workspace identity
and schema, and refuses to overwrite retained records; read `restore --help` and the protocol before using it. `migrate` imports
selected legacy sources with provenance verification and activation fencing; it
is an explicit offline migration, not a requirement for a fresh installation.

`service remove --apply` removes the macOS service, retaining workspace data.
Uninstall the executable with `pipx uninstall agentcoord` or
`brew uninstall local/agentcoord/agentcoord`. Review and remove only Agentcoord's
marked instruction blocks and MCP/lifecycle entries if removing integrations;
preserve unrelated tools, hooks and instructions.

## Optional Herdr monitor

`agentcoord monitor` works directly in a terminal. For a Herdr tab, generate
candidates with `init --candidate-dir` and install the generated
`herdr-plugin.toml` and `herdr.sh` through Herdr's plugin mechanism. The generated
launcher contains validated executable paths; do not copy the unrendered source
template. Its `Agent coordination: open monitor` action routes to Herdr's selected
workspace. The monitor is read-only and does not consume agent messages.

## Development and release builds

```sh
git clone https://github.com/andrewfitz/agentcoord.git
cd agentcoord
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: 'setuptools>=77' pip pytest ruff build
.venv/bin/python -m pip install --only-binary=:all: --no-build-isolation -e .
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Keep task notes and raw logs in ignored `.agent-work/<task-id>/`. Follow
[AGENTS.md](AGENTS.md) when contributing. Tests exercise actual service, CLI and
MCP boundaries, visibility, lifecycle, decisions, readiness, Git recovery and
installation. Native client activation needs its own acceptance checks; unit
tests alone cannot certify every harness/version.

Build a release in a **new** directory outside the checkout:

```sh
.venv/bin/python scripts/build_release.py \
  --output /absolute/path/to/new-release \
  --formula-output /absolute/path/to/new-release/agentcoord.rb \
  --installation-url https://github.com/andrewfitz/agentcoord/releases/download/vNEXT/agentcoord-install.tar.gz
```

`release-lock.json` pins complete dependencies and official PyPI wheel hashes for
the supported ABI/platform. Dependency changes, missing wheels, incompatible
targets and failed checksums stop the build. There is no source-compilation
fallback. The wheel cache lives at `$XDG_CACHE_HOME/agentcoord/release-wheels` or
`~/.cache/agentcoord/release-wheels`; every hit is checksum-verified. Application
updates reuse those dependencies.

The builder emits the application wheel, source archive, installation bundle,
formula and local `release.json` receipt. Formula installs are offline and
hash-checked. `--installation-url` sets the bundle download URL;
`--source-url` records source provenance only. Neither flag uploads anything.
Upload artifacts only after verification; retain each released archive unchanged.
Local build receipts can contain machine paths and should stay local.

Questions and defects: [GitHub Issues](https://github.com/andrewfitz/agentcoord/issues).
