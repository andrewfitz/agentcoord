# Agentcoord

Agentcoord coordinates agents working in a shared Git checkout. It keeps scoped
activity, directed conversations, explicit decisions, artifact readiness, safe
commit execution and scheduled work in one local service and SQLite database.

The package is independent of the repository hosting its source. Each workspace
has private local state. CLI, MCP and the terminal monitor use the same service.
macOS supports a managed service; Linux supports a foreground service.

Agents report meaningful work and handoffs. Reads and ordinary shell commands
need no declarations, messages or coordination wrappers. Folder scopes help
discover relevant work; they do not lock files or grant overwrite permission.
Silence, presence and message handling never resolve a decision.

## Install and initialize

Install a built wheel into an isolated Python 3.11+ environment, or use the
generated Homebrew formula in a local tap. `scripts/build_release.py --help`
describes immutable source releases and pinned dependency resources. Keep each
release archive available at its recorded URL; update the formula for subsequent
`brew upgrade` releases. Building a release does not publish or install it.

From the repository to register:

```sh
agentcoord init --apply
agentcoord init --candidate-dir /absolute/path/to/reviewed-configs
```

The first command registers the workspace and installs the optional configuration
and instruction block. The second generates inert MCP and lifecycle candidates.
Merge the applicable candidates into the harness configuration, preserving
unrelated tools and hooks. Start the foreground service with `agentcoord serve`
on macOS or Linux. On macOS, `agentcoord service install --apply` installs and
starts the workspace's launchd service as an alternative to foreground operation.
Without `--apply`, `agentcoord service install` only previews the installation.
`agentcoord doctor --live` distinguishes configuration from observed connectivity.

Run commands inside the workspace or supply `--project /absolute/repository`.
Private state lives outside the checkout under `AGENTCOORD_STATE_HOME`,
`$XDG_STATE_HOME/agentcoord`, or `~/.local/state/agentcoord`. A stable workspace ID
separates unrelated repositories. `.agentcoord.toml` holds optional configuration,
including an explicit version-bump rule if wanted.

## Agent workflow

1. Publish `activity` at a meaningful scope, test, blocker or handoff boundary.
   Record intended behavior and useful paths. Reads need no declarations.
2. Before overlapping edits, inspect the current diff and relevant
   `activities`/`evidence`. Preserve intentional repairs. Send a directed message
   only when another agent needs to act on a real overlap or dependency.
3. Use `request-find` before making a blocking `request`. Follow an existing
   decision when it covers the same dependency. Continue independent work while
   waiting; only an explicit answer resolves the decision.
4. Handle the bounded action digest at phase boundaries. `sync` presents actions,
   `message` reads full content and `consume` marks a message handled. None of
   these answers a decision or accepts artifact readiness.
5. Use `handoff` or `ready` with evidence for a usable shared artifact. Consumers
   inspect and accept its hash-bound receipt before dependent integration.
6. Use `commit execute` for owned files, or a reviewed patch with its SHA-256 and
   full base commit for owned hunks in mixed files. Only exact selected paths
   are reserved. Disjoint preparation runs concurrently; the final Git update
   briefly serializes. Tests never run from or hold the commit window.

```sh
agentcoord activity --task parser-repair --paths src/parser --state working --note 'Preserve escaped delimiters'
agentcoord sync
agentcoord commit execute --paths src/parser/tokenize.py --message 'Fix escaped delimiters'
agentcoord monitor
```

Mutations have actor-scoped retry keys. Reuse the key after an uncertain transport
outcome, and inspect `receipt` or `operation get` before repeating an external
effect. Slow commands normally wait for a terminal receipt in the adapter;
`--no-wait` returns the durable operation ID. A queued receipt is not completion.

See [protocol.md](protocol.md) for message content, lifecycle, handoff and recovery
rules. Each command exposes its actual fields through `--help`.

## Identity and delivery

Native adapters use real harness sessions and process-start evidence. A shared
MCP connection identifies the native group and cannot impersonate a child.
Protected lifecycle, Git and resume actions use the native-owner CLI or an
independently bound child. Labels, pane titles and transcripts never select the
sender. Missing native evidence fails explicitly.

Messages are database records retrieved through tools, never terminal input.
Agents do not send heartbeats or poll inboxes. The daemon observes native process
state separately from reported work. Historical activity is not live presence;
an offline actor does not grant permission to overwrite its edits.

A message does not wake a stopped model. Reminders and explicitly consented
native resumes are separate jobs. Resume requires a verified offline target and
unchanged authority. Unsupported adapters fail explicitly; paused, completed,
live and ambiguous targets are not restarted.

The OS user is the trust boundary. Private sockets and connection binding prevent
accidental identity mixing; they do not sandbox malicious code run by that user.

## Operation and recovery

The read-only monitor shows actors, actions, work and complete paginated history
without consuming messages. Default agent digests have at most three previews;
full records remain available on demand.

A normal service restart preserves work. Explicit maintenance drain stops new
mutations and external starts, permits outcome inspection and waits for owned
work. Uncertain Git publications or process launches require reconciliation
before retry.

`backup`, `restore` and `migrate` are offline maintenance commands using the same
exclusive service-ownership lock. Restore refuses to overwrite retained records.
Import preserves provenance and verifies every mapped record and relationship;
it stays fenced until explicitly activated. Service removal retains the database.

Use `doctor --live` to check an installed workspace. Release verification should
distinguish package tests from actual native-client, platform and migration
checks; an unavailable check is not a passing result.

## Develop and release

Run development commands from this repository's root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest ruff build
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m build
```

`scripts/build_release.py` builds an immutable wheel, source archive and pinned
Homebrew formula. Supply an unused release directory and the formula destination;
keep the resulting archives available at their recorded URLs. Install the
formula through a local tap for local releases. Publishing a remote repository
or release is a separate action.
