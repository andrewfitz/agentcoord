"""MCP SDK boundary; each stdio server retains one native socket binding."""

from __future__ import annotations

import json
import sys

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .cli import BY_TOOL, CATALOG, invoke, native_context
from .core import CoordinationError
from .transport import MAX_FRAME, Client, error_envelope


class BoundedInput:
    def __init__(self, binary):
        self.binary = binary

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await anyio.to_thread.run_sync(
            self.binary.readline, MAX_FRAME + 1, abandon_on_cancel=True
        )
        if not data:
            raise StopAsyncIteration
        if len(data) > MAX_FRAME or not data.endswith(b"\n"):
            raise ValueError("MCP frame exceeds 256 KiB or lacks newline framing")
        return data.decode("utf-8", errors="strict")


class BoundedOutput:
    def __init__(self, binary):
        self.binary = binary

    async def write(self, text):
        data = text.encode("utf-8")
        if len(data) > MAX_FRAME:
            raise ValueError("MCP frame exceeds 256 KiB; request a smaller page")
        await anyio.to_thread.run_sync(self.binary.write, data)

    async def flush(self):
        await anyio.to_thread.run_sync(self.binary.flush)


def tool_result(envelope):
    result = types.CallToolResult(
        content=[
            types.TextContent(
                type="text", text=json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            )
        ],
        is_error=not envelope.get("ok", False),
    )
    if len(result.model_dump_json().encode("utf-8")) > MAX_FRAME - 512:
        bounded = error_envelope(
            "INVALID_ARGUMENT", "MCP response exceeds 256 KiB; request a smaller chunk or page"
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(bounded, separators=(",", ":")))], is_error=True
        )
    return result


def create_server(client):
    async def list_tools(context, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec.tool,
                    description=spec.description,
                    input_schema=spec.schema(),
                    annotations=types.ToolAnnotations(
                        read_only_hint=not spec.mutation,
                        destructive_hint=False,
                        idempotent_hint=False,
                        open_world_hint=False,
                    ),
                )
                for spec in CATALOG
            ]
        )

    async def call_tool(context, params):
        name, arguments = params.name, params.arguments
        spec = BY_TOOL.get(name)
        if spec is None:
            return tool_result(
                error_envelope("INVALID_ARGUMENT", "Unknown coordination capability")
            )
        try:
            envelope = await anyio.to_thread.run_sync(lambda: invoke(client, spec, arguments or {}))
        except CoordinationError as exc:
            envelope = error_envelope(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                details=exc.details,
                next_action=exc.next_action,
            )
        return tool_result(envelope)

    return Server("agentcoord", on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(client, *, stdin=None, stdout=None):
    server = create_server(client)
    source = stdin or BoundedInput(sys.stdin.buffer)
    destination = stdout or BoundedOutput(sys.stdout.buffer)
    async with stdio_server(source, destination) as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run(workspace, *, harness=None):
    # Capture before serving. Tool arguments and pane changes cannot replace it.
    from .config import load_config

    context = native_context(harness, executable_paths=load_config(workspace).native_executables)
    client = Client(workspace.socket_path, context, workspace_id=workspace.id, transport="mcp")
    try:
        # Hosts may negotiate MCP before SessionStart registers this captured
        # native process. Bind once when the first tool invocation reaches call.
        anyio.run(serve, client)
    finally:
        client.close()
