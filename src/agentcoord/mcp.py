"""MCP SDK boundary; each stdio server retains one native socket binding."""

from __future__ import annotations

import json
import secrets
import sys
import uuid
from pathlib import Path

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from .cli import BY_TOOL, CATALOG, invoke, native_context
from .core import CoordinationError, identifier
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


class ChannelReceiver:
    """Event-driven push on the existing Claude MCP stream, no model polling."""

    def __init__(self, client, write, group):
        self.client, self.write, self.group = client, write, group
        self.listener = self.path = self.route = None
        self.lock = anyio.Lock()
        self.attempted = False
        self.retry_after_bind = False
        self.seen = set()
        self.revision = None

    async def start(self, *, bound=False):
        async with self.lock:
            revision = getattr(self.client, 'binding_revision', None)
            if self.listener and revision != self.revision:
                await self.close()
                self.listener = self.path = self.route = None
                self.attempted = False
            if self.listener or (self.attempted and not (bound and self.retry_after_bind)):
                return
            self.attempted = True
            self.retry_after_bind = False
            try:
                reply = await anyio.to_thread.run_sync(lambda: self.client.call('wake.channel', {}, key=str(uuid.uuid4())))
            except CoordinationError as error:
                self.retry_after_bind = error.code == 'UNBOUND_ACTOR' and not bound
                raise
            if not reply.get('ok'):
                # A hook may not have registered native startup yet. A later
                # successful native tool binding is a concrete setup change.
                self.retry_after_bind = reply.get('error', {}).get('code') == 'UNBOUND_ACTOR' and not bound
                return
            self.route = reply['data']
            self.path = Path(self.route['path'])
            self.listener = await anyio.create_unix_listener(self.path, mode=0o600, backlog=8)
            self.revision = getattr(self.client, 'binding_revision', None)
            self.group.start_soon(self.listen, self.listener)

    async def listen(self, listener):
        try:
            await listener.serve(self.receive)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return

    async def receive(self, stream):
        async with stream:
            try:
                with anyio.fail_after(4):
                    frame = bytearray()
                    while not frame.endswith(b'\n') and len(frame) < 1024:
                        frame.extend(await stream.receive(1024 - len(frame)))
                    payload = json.loads(frame)
                    if not secrets.compare_digest(payload.get('nonce', ''), self.route['nonce']):
                        return
                    oid = identifier(payload.get('operation_id'), 'operation_id')
                    if oid not in self.seen:
                        await self.write.send(SessionMessage(types.JSONRPCNotification(
                            jsonrpc='2.0', method='notifications/claude/channel', params={
                                'content': 'Agentcoord has pending directed messages. Run agentcoord sync once and handle relevant messages within your existing scope. No courtesy acknowledgment or polling.',
                                'meta': {'wake_id': oid},
                            })))
                        # Bounded same-connection suppression; durable uncertainty
                        # prevents automatic replays across process restarts.
                        if len(self.seen) >= 128:
                            self.seen.clear()
                        self.seen.add(oid)
                    await stream.send(b'{"delivered":true}\n')
            except (CoordinationError, ValueError, TypeError, TimeoutError, anyio.EndOfStream,
                    anyio.BrokenResourceError, anyio.ClosedResourceError):
                return

    async def close(self):
        if self.listener:
            await self.listener.aclose()
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def create_server(client, *, channel=None):
    async def list_tools(context, params):
        if channel:
            try:
                await channel.start()
            except (CoordinationError, OSError):
                pass  # Channel availability cannot block the ordinary tool catalog.
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
            if channel and envelope.get('ok'):
                try:
                    await channel.start(bound=True)
                except (CoordinationError, OSError):
                    pass
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


async def serve(client, *, stdin=None, stdout=None, channel=False):
    source = stdin or BoundedInput(sys.stdin.buffer)
    destination = stdout or BoundedOutput(sys.stdout.buffer)
    async with stdio_server(source, destination) as (read, write):  # noqa: SIM117 — transport owns stream lifetime.
        async with anyio.create_task_group() as group:
            receiver = ChannelReceiver(client, write, group) if channel else None
            server = create_server(client, channel=receiver)
            options = server.create_initialization_options(
                experimental_capabilities={'claude/channel': {}} if channel else None)
            try:
                await server.run(read, write, options)
            finally:
                if receiver:
                    with anyio.CancelScope(shield=True):
                        await receiver.close()
                group.cancel_scope.cancel()


def run(workspace, *, harness=None):
    # Capture before serving. Tool arguments and pane changes cannot replace it.
    from .config import load_config
    from .wake_adapters import claude_channel_requested

    context = native_context(harness, executable_paths=load_config(workspace).native_executables)
    channel = claude_channel_requested(context)
    client = Client(workspace.socket_path, context, workspace_id=workspace.id, transport="mcp")
    try:
        # Hosts may negotiate MCP before SessionStart registers this captured
        # native process. Bind once when the first tool invocation reaches call.
        async def serving():
            await serve(client, channel=channel)
        anyio.run(serving)
    finally:
        client.close()
