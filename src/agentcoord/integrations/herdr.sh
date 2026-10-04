#!/bin/sh
# One user-wide plugin; Herdr's selected workspace is the only project authority.
# Python is addressed through the stable executable prefix, not a versioned keg.
exec @ENVIRONMENT@ @PYTHON@ -c '
import json
import os
from pathlib import Path
import sys

def refuse(message):
    print("agentcoord Herdr: " + message, file=sys.stderr)
    raise SystemExit(2)

def unique_fields(items):
    result = {}
    for key, value in items:
        if key in result:
            refuse("duplicate invocation-context field")
        result[key] = value
    return result

try:
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", ""), object_pairs_hook=unique_fields)
except ValueError:
    refuse("valid HERDR_PLUGIN_CONTEXT_JSON is required")
if not isinstance(context, dict):
    refuse("invocation context must be an object")
workspace = context.get("workspace_id")
project = context.get("workspace_cwd")
if not isinstance(workspace, str) or not workspace or workspace != os.environ.get("HERDR_WORKSPACE_ID"):
    refuse("workspace context is missing or disagrees with Herdr")
if not isinstance(project, str) or not project or not Path(project).is_absolute():
    refuse("selected workspace must supply an absolute workspace_cwd")
try:
    project = str(Path(project).resolve(strict=True))
except (OSError, ValueError):
    refuse("selected workspace directory is unavailable")
if not Path(project).is_dir():
    refuse("selected workspace must be a directory")
environment = dict(os.environ)
environment["AGENTCOORD_WORKSPACE"] = project
executable = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) == 3 else "monitor" if len(sys.argv) == 2 else None
if mode == "open":
    herdr = environment.get("HERDR_BIN_PATH")
    if not herdr or not Path(herdr).is_absolute():
        refuse("Herdr must supply its absolute HERDR_BIN_PATH")
    arguments = [herdr, "plugin", "pane", "open", "--plugin", "agentcoord",
                 "--entrypoint", "monitor", "--placement", "tab",
                 "--workspace", workspace, "--cwd", project, "--focus"]
    os.execvpe(herdr, arguments, environment)
elif mode == "monitor":
    os.execvpe(executable, [executable, "--project", project, "monitor"], environment)
else:
    refuse("expected open or monitor")
' @EXECUTABLE@ "$@"
