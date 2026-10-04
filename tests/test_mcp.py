"""Actual MCP SDK negotiation, shared schemas and structured error boundaries."""

import io
import json

import anyio
from mcp import ClientSession

from agentcoord.mcp import BoundedInput, create_server, tool_result
from agentcoord.transport import MAX_FRAME

MESSAGE_ID = "36f07549-04fb-4f83-b43a-b326ae64983b"


def test_real_sdk_catalog_calls_and_rejected_identity_override():
    class Client:
        def __init__(self):
            self.calls = []

        def call(self, operation, arguments, key):
            self.calls.append((operation, arguments, key))
            return {
                "ok": True,
                "protocol": 1,
                "request_id": "request",
                "data": {"handled": True},
                "action_digest": None,
            }

    async def exercise():
        client = Client()
        server = create_server(client)
        client_send, server_read = anyio.create_memory_object_stream(4)
        server_send, client_read = anyio.create_memory_object_stream(4)
        async with anyio.create_task_group() as group:
            group.start_soon(
                server.run, server_read, server_send, server.create_initialization_options()
            )
            async with ClientSession(client_read, client_send) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "agentcoord"
                tools = await session.list_tools()
                assert client.calls == []
                names = {tool.name for tool in tools.tools}
                assert {
                    "activity",
                    "send",
                    "request",
                    "request_follow",
                    "consume",
                    "dependency_accept",
                    "commit_execute",
                    "schedule",
                    "receipt",
                    "operation_ack",
                    "commit_cancel",
                } <= names
                assert "identity_bind" not in names and "service_drain" not in names
                reply = await session.call_tool("consume", {"id": MESSAGE_ID, "key": "retry-key"})
                envelope = json.loads(reply.content[0].text)
                assert envelope["ok"] and client.calls == [
                    ("message.consume", {"id": MESSAGE_ID}, "retry-key")
                ]
                rejected = await session.call_tool(
                    "consume", {"id": "message", "actor_id": "other"}
                )
                assert rejected.is_error
                assert len(client.calls) == 1
            group.cancel_scope.cancel()

    anyio.run(exercise)


def test_mcp_bounds_raw_input_before_sdk_allocation():
    async def exercise():
        source = BoundedInput(io.BytesIO(b"x" * (MAX_FRAME + 1) + b"\n"))
        try:
            await source.__anext__()
        except ValueError as error:
            assert "256 KiB" in str(error)
        else:
            raise AssertionError("Oversize input reached SDK parsing")

    anyio.run(exercise)


def test_large_mcp_output_returns_explicit_chunk_error():
    result = tool_result({"ok": True, "data": {"body": "x" * MAX_FRAME}})
    assert result.is_error
    envelope = json.loads(result.content[0].text)
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "smaller" in envelope["error"]["message"]
