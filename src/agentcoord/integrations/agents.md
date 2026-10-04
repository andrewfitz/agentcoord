Use agentcoord for meaningful scope updates, real dependencies and handoffs.
At a work boundary, inspect relevant activity and current diffs, then use `sync`
for pending actions. Read full content by ID before acting. Explicitly consume
handled messages, resolve decisions and accept reviewed readiness separately.
Folder scopes describe work; they do not reserve edits. Silence and expired
presence never authorize overwriting. Keep independent work moving while waiting.
Native lifecycle hooks observe presence; ordinary commands need no hook or ping.
Use exact paths or a reviewed patch for shared-checkout commits and preserve peer
edits and staging. Operator monitor reads never handle another actor's messages.
Run `agentcoord --help` for the installed commands and `agentcoord doctor --live`
when configuration or connection needs diagnosis.
