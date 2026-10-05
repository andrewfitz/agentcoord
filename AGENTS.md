# Agentcoord contributor instructions

Agentcoord is a standalone Python package for agents sharing a Git checkout.
Keep its runtime, configuration, integrations and documentation independent of
the repository in which it is installed. README.md describes setup and checks;
protocol.md owns coordination behavior.

- Preserve intentional concurrent edits. Publish meaningful coding scope,
  blockers and handoffs through the installed Agentcoord service when the
  workspace is registered. Reads and ordinary commands need no coordination.
- Inspect current diffs and relevant activity before overlapping edits. Ask an
  owner only when a real shared contract or intended change remains unclear.
  Continue independent work while waiting; silence grants no authority.
- Keep one local service and SQLite authority per registered workspace. CLI,
  MCP, monitor and integrations use the same typed application handlers.
  Never add a repository-specific import, path, command or fallback.
- Preserve authenticated native identity, exact generations, retry keys,
  durable decisions and external-effect recovery. Missing required evidence
  fails explicitly. Unknown presence cannot authorize overwrite or resume.
- Messages are tool-retrieved records. Do not inject terminal input, poll an
  inbox, announce reads, add per-command hooks or require routine acknowledgments.
- Keep task notes and raw logs in ignored `.agent-work/<task-id>/`. Version
  maintained source, tests, protocol, integration resources and release tooling.
- Run affected tests after changes settle. A file move needs path and packaging
  checks; unchanged behavior does not require repeating load/platform matrices.
  Use `python -m pytest` and `ruff check .` in the development environment.
- Commit only owned paths or reviewed owned hunks. Use installed Agentcoord
  commit execution when bound; tests never hold its commit window. Preserve
  unrelated staged work. Do not push or publish without user authorization.
- If coordination is unreachable, continue independent work and authorized
  fully owned-file commits using the protocol's direct Git outage procedure.
  Stop unchanged retries; defer only real overlaps or uncertain prior publication.
- Release archives are immutable. Build a new release and update the Homebrew
  formula for a package change; never overwrite a retained release artifact.
  Source readiness, installed verification and live activation are separate.

Use `agentcoord --help` and protocol.md for the actual command contracts.
