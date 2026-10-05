"""One explicit command/schema catalog for the native CLI and MCP adapters."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .core import CoordinationError, identifier, validate_fields
from .transport import Client, encode_frame, error_envelope

TEXT = {"type": "string", "minLength": 1, "maxLength": 65536}
ID = {"type": "string", "format": "uuid"}
BOOL = {"type": "boolean"}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
TIME = {"type": "integer", "minimum": 0}
JOB_TIME = {**TIME, "maximum": 253402300799999999}
PATHS = {"type": "array", "items": {**TEXT, "maxLength": 4096}, "minItems": 1, "maxItems": 100}
IDS = {"type": "array", "items": ID, "minItems": 1, "maxItems": 16}
PAGE = {"limit": LIMIT, "after": TEXT}
SCOPED = {"paths": PATHS, "limit": LIMIT, "after": TEXT}
STATE = {**TEXT, "enum": ["working", "idle", "waiting", "blocked", "paused", "completed"]}
OPERATOR_FILTERS = {
    "type": "object",
    "properties": {
        "actor_id": {"type": "string", "format": "uuid"},
        "task": {**TEXT, "maxLength": 4096},
        "path": {**TEXT, "maxLength": 4096},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class CommandSpec:
    operation: str
    command: tuple[str, ...]
    tool: str
    description: str
    properties: dict
    required: tuple[str, ...] = ()
    mutation: bool = False
    positional: tuple[str, ...] = ()

    def schema(self):
        properties = dict(self.properties)
        if self.mutation:
            properties["key"] = {
                **TEXT,
                "maxLength": 256,
                "description": "Stable retry key; retain it after an uncertain response.",
            }
        return {
            "type": "object",
            "properties": properties,
            "required": list(self.required),
            "additionalProperties": False,
        }


def _spec(
    operation,
    command,
    tool,
    description,
    properties=None,
    required=(),
    mutation=False,
    positional=(),
):
    return CommandSpec(
        operation,
        tuple(command.split()),
        tool,
        description,
        properties or {},
        tuple(required),
        mutation,
        tuple(positional),
    )


CATALOG = (
    _spec(
        "identity.get",
        "identity",
        "identity",
        "Inspect this connection's native identity and attribution limits.",
    ),
    _spec(
        "identity.delegate",
        "delegate",
        "delegate",
        "Consent to scoped parent routing for an independently registered child.",
        {"child_id": ID},
        ("child_id",),
        True,
        ("child_id",),
    ),
    _spec(
        "identity.checkpoint",
        "checkpoint",
        "checkpoint",
        "Save a meaningful checkpoint and explicit resume preference.",
        {"state": STATE, "note": TEXT, "resume_enabled": BOOL},
        ("state", "note"),
        True,
    ),
    _spec(
        "identity.complete",
        "complete",
        "complete",
        "Complete the bound actor's work explicitly.",
        {"note": TEXT},
        ("note",),
        True,
    ),
    _spec(
        "identity.status",
        "status",
        "status",
        "Inspect observed presence separately from reported work.",
        {"all": BOOL, "limit": LIMIT, "after": ID},
    ),
    _spec(
        "work.activity",
        "activity",
        "activity",
        "Report new work, a changed scope, a blocker or an outcome once.",
        {
            "task": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "note": {"type": "string", "maxLength": 2000},
            "state": {
                "type": "string",
                "enum": ["working", "idle", "blocked", "paused", "completed"],
            },
            "evidence": {},
        },
        (),
        True,
        (),
    ),
    _spec(
        "work.activities",
        "activities",
        "activities",
        "Find relevant reported work; paths are discovery labels.",
        {
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "after": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "current": {"type": "boolean"},
        },
        (),
        False,
        (),
    ),
    _spec(
        "work.intent",
        "intent",
        "intent",
        "Record an intentional change and invariants without reserving edits.",
        {
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "purpose": {"type": "string", "maxLength": 2000},
            "invariants": {
                "type": "array",
                "items": {"type": "string", "maxLength": 2000},
                "maxItems": 32,
            },
            "state": {"type": "string", "enum": ["active", "completed", "withdrawn"]},
        },
        ("paths", "purpose", "invariants"),
        True,
        (),
    ),
    _spec(
        "work.evidence",
        "evidence",
        "evidence",
        "Find relevant recorded intent, outcomes and evidence before asking peers.",
        {
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "intents_after": {"type": "string", "format": "uuid"},
        },
        ("paths",),
        False,
        (),
    ),
    _spec(
        "work.evidence_detail",
        "evidence-detail",
        "evidence_detail",
        "Retrieve a complete evidence record by its returned opaque ID.",
        {
            "kind": {"type": "string", "enum": ["activity", "intent", "change", "decision", "readiness"]},
            "id": {"type": "string", "format": "uuid"},
            "paths_after": {"type": "string", "maxLength": 4096},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ("kind", "id"),
        False,
        (),
    ),
    _spec(
        "message.send",
        "send",
        "send",
        "Send a directed message using the bound sender and a stable retry key.",
        {
            "recipients": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
                "minItems": 1,
                "maxItems": 16,
                "uniqueItems": True,
            },
            "kind": {"type": "string", "maxLength": 40},
            "subject": {"type": "string", "maxLength": 1024},
            "body": {"type": "string", "maxLength": 65536},
            "thread": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "context": {"type": "object"},
            "requested_ack": {"type": "boolean"},
        },
        ("recipients", "kind", "subject", "body"),
        True,
        (),
    ),
    _spec(
        "message.get",
        "message",
        "message",
        "Read a complete message in explicit bounded UTF-8 chunks.",
        {
            "id": {"type": "string", "format": "uuid"},
            "offset": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "limit": {"type": "integer", "minimum": 1, "maximum": 65536},
        },
        ("id",),
        False,
        ("id",),
    ),
    _spec(
        "message.attachments",
        "attachments",
        "attachments",
        "Page through bounded attachment metadata for a visible message.",
        {
            "id": ID,
            "after": ID,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ("id",),
        False,
        ("id",),
    ),
    _spec(
        "message.attachment",
        "attachment",
        "attachment",
        "Read one complete retained attachment reference in UTF-8 chunks.",
        {
            "id": ID,
            "offset": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "limit": {"type": "integer", "minimum": 1, "maximum": 32768},
        },
        ("id",),
        False,
        ("id",),
    ),
    _spec(
        "message.consume",
        "consume",
        "consume",
        "Mark the caller's message handled; decisions remain separate.",
        {"id": {"type": "string", "format": "uuid"}},
        ("id",),
        True,
        ("id",),
    ),
    _spec(
        "message.consume_batch",
        "consume-batch",
        "consume_batch",
        "Handle selected messages and inspect individual failures.",
        {
            "ids": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
                "minItems": 1,
                "maxItems": 100,
            }
        },
        ("ids",),
        True,
        ("ids",),
    ),
    _spec(
        "message.ack",
        "ack",
        "ack",
        "Record a requested receipt without creating an acknowledgment conversation.",
        {"id": {"type": "string", "format": "uuid"}},
        ("id",),
        True,
        ("id",),
    ),
    _spec(
        "message.inbox",
        "inbox",
        "inbox",
        "Peek at pending work without presentation or handling.",
        {
            "after": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "new_only": {"type": "boolean"},
        },
        (),
        False,
        (),
    ),
    _spec(
        "message.history",
        "history",
        "history",
        "Traverse complete conversation history with an explicit continuation.",
        {
            "cursor": {"type": "string", "maxLength": 2048},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "actor": {"type": "string", "format": "uuid"},
            "task": {"type": "string", "maxLength": 256},
            "thread": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "since_us": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "until_us": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "text": {"type": "string", "maxLength": 512},
        },
        (),
        False,
        (),
    ),
    _spec(
        "message.search",
        "search",
        "search",
        "Search retained conversations without consuming messages.",
        {
            "cursor": {"type": "string", "maxLength": 2048},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "actor": {"type": "string", "format": "uuid"},
            "task": {"type": "string", "maxLength": 256},
            "thread": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "since_us": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "until_us": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "text": {"type": "string", "maxLength": 512},
        },
        (),
        False,
        (),
    ),
    _spec(
        "message.sync",
        "sync",
        "sync",
        "Present a bounded action digest at a natural work boundary.",
        {"limit": {**LIMIT, "maximum": 3}, "cursor": TEXT, "new_only": BOOL},
        (),
        True,
    ),
    _spec(
        "decision.request",
        "request",
        "request",
        "Track one scoped dependency decision; discover existing requests first.",
        {
            "recipient": {"type": "string", "format": "uuid"},
            "subject": {"type": "string", "maxLength": 1024},
            "body": {"type": "string", "maxLength": 65536},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "deadline_us": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
        },
        ("recipient", "subject", "body", "paths"),
        True,
        (),
    ),
    _spec(
        "decision.find",
        "request-find",
        "request_find",
        "Find covering open decisions before creating a duplicate.",
        {
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "after": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
        },
        ("paths",),
        False,
        (),
    ),
    _spec(
        "decision.get",
        "request-get",
        "request_get",
        "Inspect the complete selected decision and exact answer proposal.",
        {
            "id": {"type": "string", "format": "uuid"},
            "offset": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "response_offset": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "limit": {"type": "integer", "minimum": 1, "maximum": 32768},
            "proposal_id": {"type": "string", "format": "uuid"},
            "proposals_after": {"type": "string", "format": "uuid"},
        },
        ("id",),
        False,
        ("id",),
    ),
    _spec(
        "decision.list",
        "requests",
        "requests",
        "Inspect actionable decisions and outgoing waits.",
        {
            "after": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        (),
        False,
        (),
    ),
    _spec(
        "decision.follow",
        "request-follow",
        "request_follow",
        "Follow a decision's updates without answer or edit authority.",
        {"id": {"type": "string", "format": "uuid"}},
        ("id",),
        True,
        ("id",),
    ),
    _spec(
        "decision.unfollow",
        "request-unfollow",
        "request_unfollow",
        "Stop following an obsolete decision.",
        {"id": {"type": "string", "format": "uuid"}},
        ("id",),
        True,
        ("id",),
    ),
    _spec(
        "decision.defer",
        "request-defer",
        "request_defer",
        "Defer the dependent slice while leaving the decision open.",
        {
            "id": {"type": "string", "format": "uuid"},
            "until_us": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "note": {"type": "string", "maxLength": 2000},
        },
        ("id", "until_us", "note"),
        True,
        ("id",),
    ),
    _spec(
        "decision.resolve",
        "request-resolve",
        "request_resolve",
        "Answer, decline or requester-cancel a decision explicitly.",
        {
            "id": {"type": "string", "format": "uuid"},
            "state": {"type": "string", "enum": ["answered", "declined", "cancelled"]},
            "response": {"type": "string", "maxLength": 65536},
            "external_settlement": {
                "type": "object",
                "properties": {
                    "answered_by": {"type": "string", "format": "uuid"},
                    "evidence": {"type": "string", "maxLength": 4096},
                },
                "required": ["answered_by", "evidence"],
                "additionalProperties": False,
            },
        },
        ("id", "state", "response"),
        True,
        ("id",),
    ),
    _spec(
        "decision.reconcile",
        "request-reconcile",
        "request_reconcile",
        "Accept or reject the exact stale-generation answer proposal.",
        {
            "id": {"type": "string", "format": "uuid"},
            "proposal_id": {"type": "string", "format": "uuid"},
            "accept": {"type": "boolean"},
        },
        ("id", "proposal_id", "accept"),
        True,
        ("id",),
    ),
    _spec(
        "decision.transfer",
        "request-transfer",
        "request_transfer",
        "Explicitly transfer a decision under current scoped authority.",
        {
            "id": {"type": "string", "format": "uuid"},
            "recipient": {"type": "string", "format": "uuid"},
            "reason": {"type": "string", "maxLength": 2000},
        },
        ("id", "recipient", "reason"),
        True,
        ("id",),
    ),
    _spec(
        "readiness.subscribe",
        "dependency subscribe",
        "dependency_subscribe",
        "Subscribe to a known artifact before dependent work.",
        {
            "producer": {"type": "string", "format": "uuid"},
            "artifact": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "next_action": {"type": "string", "maxLength": 2000},
            "task": {"type": "string", "maxLength": 256},
        },
        ("producer", "artifact", "paths", "next_action"),
        True,
        (),
    ),
    _spec(
        "readiness.cancel",
        "dependency cancel",
        "dependency_cancel",
        "Cancel an obsolete artifact subscription.",
        {"id": {"type": "string", "format": "uuid"}},
        ("id",),
        True,
        ("id",),
    ),
    _spec(
        "readiness.publish",
        "ready",
        "ready",
        "Publish complete hash-bound artifact readiness with evidence.",
        {
            "artifact": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "evidence": {},
            "status": {"type": "string", "enum": ["ready", "verified"]},
        },
        ("artifact", "paths", "evidence"),
        True,
        (),
    ),
    _spec(
        "readiness.withdraw",
        "ready-withdraw",
        "ready_withdraw",
        "Withdraw obsolete readiness explicitly.",
        {
            "artifact": {"type": "string", "maxLength": 256},
            "reason": {"type": "string", "maxLength": 2000},
        },
        ("artifact", "reason"),
        True,
        (),
    ),
    _spec(
        "readiness.inspect",
        "readiness",
        "readiness",
        "Inspect the latest receipt against every observed input.",
        {
            "producer": {"type": "string", "format": "uuid"},
            "artifact": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "after": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
        },
        (),
        True,
        (),
    ),
    _spec(
        "readiness.updates",
        "dependency updates",
        "dependency_updates",
        "Inspect pending dependency updates or traverse explicit history.",
        {
            "after": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "history": {"type": "boolean"},
        },
        (),
        False,
        (),
    ),
    _spec(
        "readiness.accept",
        "dependency accept",
        "dependency_accept",
        "Accept a reviewed current update after complete hash checks.",
        {"id": {"type": "string", "format": "uuid"}},
        ("id",),
        True,
        ("id",),
    ),
    _spec(
        "readiness.handoff",
        "handoff",
        "handoff",
        "Publish one complete readiness receipt and directed notification atomically.",
        {
            "artifact": {"type": "string", "maxLength": 256},
            "paths": {
                "type": "array",
                "items": {"type": "string", "maxLength": 4096},
                "maxItems": 10000,
                "uniqueItems": True,
            },
            "evidence": {},
            "status": {"type": "string", "enum": ["ready", "verified"]},
            "recipients": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
                "minItems": 1,
                "maxItems": 16,
                "uniqueItems": True,
            },
            "subject": {"type": "string", "maxLength": 1024},
            "body": {"type": "string", "maxLength": 65536},
            "thread": {"type": "string", "maxLength": 256},
        },
        ("artifact", "paths", "evidence", "recipients", "subject", "body"),
        True,
        (),
    ),
    _spec(
        "commit.status",
        "commit status",
        "commit_status",
        "Inspect commit admission without joining the queue.",
    ),
    _spec(
        "commit.acquire",
        "commit acquire",
        "commit_acquire",
        "Acquire a manual shared-index grant; inspect the granted receipt.",
        {},
        (),
        True,
    ),
    _spec(
        "commit.cancel",
        "commit cancel",
        "commit_cancel",
        "Cancel only an exact owned pending commit admission that has not been granted.",
        {"admission_id": ID},
        ("admission_id",),
        True,
    ),
    _spec(
        "commit.release",
        "commit release",
        "commit_release",
        "Release only the exact owned grant.",
        {"grant_id": ID},
        ("grant_id",),
        True,
    ),
    _spec(
        "commit.execute",
        "commit execute",
        "commit_execute",
        "Commit exact owned paths or a reviewed patch through a private index.",
        {
            "paths": PATHS,
            "message": TEXT,
            "bump_version": BOOL,
            "adopt_staged": BOOL,
            "patch": TEXT,
            "patch_file": TEXT,
            "patch_sha256": {**TEXT, "minLength": 64, "maxLength": 64},
            "base_commit": TEXT,
        },
        ("paths", "message"),
        True,
    ),
    _spec(
        "commit.reconcile",
        "commit reconcile",
        "commit_reconcile",
        "Inspect an uncertain Git publication before retrying.",
        {"operation_id": ID},
        ("operation_id",),
        True,
        ("operation_id",),
    ),
    _spec(
        "job.schedule",
        "schedule",
        "schedule",
        "Schedule a reminder or explicitly enabled native resume.",
        {
            "kind": {**TEXT, "enum": ["reminder", "resume"]},
            "due_us": JOB_TIME,
            "note": {**TEXT, "maxLength": 8192},
            "subject": {**TEXT, "maxLength": 512},
            "delivery_deadline_us": JOB_TIME,
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
        },
        ("kind", "due_us", "note"),
        True,
    ),
    _spec(
        "job.list",
        "jobs",
        "jobs",
        "Inspect scheduled work and uncertain effects.",
        {
            "limit": LIMIT,
            "after": ID,
            "state": {
                **TEXT,
                "enum": ["pending", "running", "succeeded", "failed", "cancelled", "uncertain"],
            },
        },
    ),
    _spec(
        "job.get",
        "job",
        "job",
        "Inspect the exact scheduled job.",
        {"id": ID},
        ("id",),
        positional=("id",),
    ),
    _spec(
        "job.cancel",
        "cancel",
        "cancel",
        "Cancel selected work without reviving an uncertain launch.",
        {"id": ID},
        ("id",),
        True,
        ("id",),
    ),
    _spec(
        "job.resolve",
        "resolve",
        "resolve",
        "Deliberately reconcile scheduled work before optional retry.",
        {
            "id": ID,
            "version": {**JOB_TIME, "minimum": 1},
            "resolution": {**TEXT, "enum": ["retry", "complete"]},
            "note": {**TEXT, "maxLength": 8192},
            "due_us": JOB_TIME,
        },
        ("id", "version", "resolution", "note"),
        True,
        ("id",),
    ),
    _spec(
        "operation.get",
        "operation get",
        "operation_get",
        "Retrieve an exact durable operation receipt.",
        {"operation_id": ID},
        ("operation_id",),
        positional=("operation_id",),
    ),
    _spec(
        "operation.list",
        "operation list",
        "operation_list",
        "Inspect the caller's durable effects.",
        {
            "limit": LIMIT,
            "after": TIME,
            "state": {
                **TEXT,
                "enum": ["queued", "running", "succeeded", "failed", "cancelled", "uncertain"],
            },
        },
    ),
    _spec(
        "operation.ack",
        "operation ack",
        "operation_ack",
        "Acknowledge an exact owned failed-operation version without retrying its effect.",
        {"operation_id": ID, "version": {"type": "integer", "minimum": 1}},
        ("operation_id", "version"),
        True,
        ("operation_id",),
    ),
    _spec(
        "operation.reconcile",
        "operation reconcile",
        "operation_reconcile",
        "Delegate an uncertain effect to its owning domain's reconciliation.",
        {"operation_id": ID},
        ("operation_id",),
        True,
        ("operation_id",),
    ),
    _spec(
        "receipt.get",
        "receipt",
        "receipt",
        "Inspect the committed result of an actor-scoped retry key.",
        {"key": {**TEXT, "maxLength": 256}},
        ("key",),
        positional=("key",),
    ),
    _spec(
        "operator.snapshot",
        "operator snapshot",
        "operator_snapshot",
        "Read current workspace work and pending decisions without consumption.",
        {
            "limit": LIMIT,
            "cursor": TEXT,
            "view": {**TEXT, "enum": ["current", "all"]},
            "section": {**TEXT, "enum": ["all", "actors", "actions", "work"]},
            "filters": OPERATOR_FILTERS,
        },
    ),
    _spec(
        "operator.history",
        "operator history",
        "operator_history",
        "Traverse the immutable workspace timeline.",
        {
            "limit": LIMIT,
            "cursor": TEXT,
            "domain": {**TEXT, "maxLength": 2048},
            "kind": {**TEXT, "maxLength": 2048},
            "actor_id": {"type": "string", "format": "uuid"},
            "query": {**TEXT, "maxLength": 2048},
            "sequence": {"type": "integer", "minimum": 1},
            "direction": {**TEXT, "enum": ["forward", "backward"]},
            "filters": OPERATOR_FILTERS,
        },
    ),
)

BY_OPERATION = {spec.operation: spec for spec in CATALOG}
BY_TOOL = {spec.tool: spec for spec in CATALOG}


def native_context(harness=None, environ=None, *, executable_paths=None):
    """Capture supported startup evidence once; pane IDs never identify an actor."""
    env = os.environ if environ is None else environ
    from .identity import native_context as capture_native

    return capture_native(harness, environ=env, executable_paths=executable_paths)


def validate_arguments(spec, arguments):
    if not isinstance(arguments, dict) or set(arguments) - set(spec.schema()["properties"]):
        raise CoordinationError("INVALID_ARGUMENT", "Unknown command fields")
    missing = set(spec.required) - set(arguments)
    if missing:
        raise CoordinationError(
            "INVALID_ARGUMENT", "Missing required fields: " + ", ".join(sorted(missing))
        )
    encode_frame(arguments)
    for name, value in arguments.items():
        _validate_value(spec.schema()["properties"][name], value, name)


def _validate_value(schema, value, name):
    kind = schema.get("type")
    invalid = False
    if kind == "string":
        invalid = not isinstance(value, str) or "\x00" in value
        if not invalid:
            invalid = (
                not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 262144)
            )
            if schema.get("format") == "uuid":
                try:
                    invalid |= str(UUID(value)) != value
                except ValueError:
                    invalid = True
    elif kind == "integer":
        invalid = type(value) is not int or not schema.get(
            "minimum", -(2**63)
        ) <= value <= schema.get("maximum", 2**63 - 1)
    elif kind == "boolean":
        invalid = type(value) is not bool
    elif kind == "array":
        invalid = not isinstance(value, list)
        if not invalid:
            invalid = not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 262144)
            if schema.get("uniqueItems"):
                invalid |= len({json.dumps(item, sort_keys=True) for item in value}) != len(value)
            for index, item in enumerate(value):
                _validate_value(schema.get("items", {}), item, f"{name}[{index}]")
    elif kind == "object":
        invalid = not isinstance(value, dict)
        if not invalid:
            properties = schema.get("properties", {})
            invalid = bool(set(schema.get("required", ())) - set(value))
            if schema.get("additionalProperties") is False:
                invalid |= bool(set(value) - set(properties))
            for field, item in value.items():
                _validate_value(properties.get(field, {}), item, f"{name}.{field}")
    if "enum" in schema:
        invalid |= value not in schema["enum"]
    if invalid:
        raise CoordinationError("INVALID_ARGUMENT", f"Invalid {name}")


def _json_argument(value):
    try:
        return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError) as exc:
        raise argparse.ArgumentTypeError("Expected valid JSON") from exc


def parser():
    root = argparse.ArgumentParser(
        prog="agentcoord", description="Directed local coordination for agents sharing a workspace."
    )
    root.add_argument(
        "--project",
        dest="root",
        type=Path,
        default=None,
        help="Explicit workspace root (otherwise discover from the current directory)",
    )
    root.add_argument(
        "--harness",
        choices=("claude", "codex", "cursor", "grok"),
        help="Use only this harness's native startup evidence",
    )
    root.add_argument(
        "--operator", action="store_true", help="Read workspace history without selecting an actor"
    )
    root.add_argument(
        "--origin-context", metavar="JSON",
        help="Require the captured workspace, actor, task and execution UUIDs for this native operation",
    )
    groups = {(): root.add_subparsers(dest="command", required=True)}
    for spec in CATALOG:
        prefix = spec.command[:-1]
        if prefix not in groups:
            parent = groups[()]
            for depth in range(1, len(prefix) + 1):
                part = prefix[:depth]
                if part not in groups:
                    group = parent.add_parser(
                        part[-1], help=f"{part[-1].capitalize()} capabilities"
                    )
                    groups[part] = group.add_subparsers(dest=f"command_{depth}", required=True)
                parent = groups[part]
        command = groups[prefix].add_parser(
            spec.command[-1], help=spec.description, description=spec.description
        )
        command.set_defaults(spec=spec)
        if spec.operation in {
            "commit.execute",
            "commit.reconcile",
            "readiness.publish",
            "readiness.accept",
            "readiness.handoff",
            "readiness.inspect",
            "operation.reconcile",
        }:
            command.add_argument(
                "--no-wait",
                action="store_true",
                help="Return the durable queued receipt immediately",
            )
            command.add_argument(
                "--wait-timeout",
                type=float,
                default=60,
                help="Maximum seconds to wait for a terminal operation",
            )
        file_fields = set(spec.properties) & {"body", "message", "patch"}
        for name, schema in spec.schema()["properties"].items():
            positional = name in spec.positional
            option = name if positional else "--" + name.replace("_", "-")
            opts = (
                {}
                if positional
                else {
                    "dest": name,
                    "required": name in spec.required and name not in file_fields,
                    "default": argparse.SUPPRESS,
                }
            )
            if not schema and name == "evidence":
                opts["required"] = False
            if schema.get("type") == "boolean":
                opts["action"] = argparse.BooleanOptionalAction
            elif schema.get("type") == "integer":
                opts["type"] = int
            elif schema.get("type") == "array":
                opts["nargs"] = "+"
            elif schema.get("type") == "object":
                opts["type"] = _json_argument
            if "enum" in schema:
                opts["choices"] = schema["enum"]
            command.add_argument(option, **opts)
            if not schema and name == "evidence":
                command.add_argument(
                    "--evidence-json",
                    type=_json_argument,
                    default=argparse.SUPPRESS,
                    help="Structured JSON evidence; use --evidence for literal text",
                )
        for field in file_fields - ({"patch"} if "patch_file" in spec.properties else set()):
            if field in spec.properties:
                command.add_argument(
                    "--" + field + "-file",
                    type=Path,
                    default=argparse.SUPPRESS,
                    help=f"Read literal UTF-8 {field} from a file",
                )
    for name in ("init", "serve", "mcp", "monitor", "doctor", "backup", "restore", "hook"):
        command = groups[()].add_parser(name)
        command.set_defaults(maintenance=name)
        if name == "init":
            command.add_argument("--candidate-dir", type=Path)
            command.add_argument("--apply", action="store_true")
        elif name == "doctor":
            command.add_argument("--live", action="store_true")
        elif name in {"backup", "restore"}:
            command.add_argument("path", type=Path)
        elif name == "hook":
            command.add_argument("harness", choices=("claude", "codex", "cursor", "grok"))
            command.add_argument(
                "event", choices=("start", "stop", "failure", "end", "child_start", "child_stop")
            )
        elif name == "mcp":
            command.add_argument(
                "--harness",
                choices=("claude", "codex", "cursor", "grok"),
                default=argparse.SUPPRESS,
            )
    service = groups[()].add_parser("service").add_subparsers(dest="service_action", required=True)
    for name in ("install", "remove", "status", "upgrade", "drain", "activate", "health"):
        command = service.add_parser(name)
        command.set_defaults(maintenance="service")
        command.add_argument("--apply", action="store_true")
    migrate = groups[()].add_parser("migrate").add_subparsers(dest="migrate_action", required=True)
    for name in ("inspect", "import", "verify"):
        command = migrate.add_parser(name)
        command.set_defaults(maintenance="migrate")
        if name == "inspect":
            command.add_argument("--sources", type=Path, required=True)
            command.add_argument("--output-manifest", type=Path, required=True)
        else:
            command.add_argument("--manifest", type=Path, required=True)
        if name == "import":
            command.add_argument(
                "--run-id",
                required=True,
                help="Stable import run ID retained across interrupted retries",
            )
    return root


def invoke(client, spec, arguments, *, context_guards=None):
    arguments = dict(arguments)
    validate_arguments(spec, arguments)
    key = arguments.pop("key", None) if spec.mutation else None
    if spec.mutation and key is None:
        key = secrets.token_hex(16)
    return client.call(spec.operation, arguments, key=key, **(context_guards or {}))


def wait_operation(client, envelope, *, timeout=60, clock=time.monotonic, sleep=time.sleep,
                   context_guards=None):
    """Wait inside the CLI while retaining one accepted operation identity."""
    if not envelope.get("ok") or not isinstance(envelope.get("data"), dict):
        return envelope
    operation_id = envelope["data"].get("operation_id")
    if not operation_id:
        return envelope
    _validate_wait_timeout(timeout)
    deadline, delay = clock() + timeout, 0.05
    while True:
        current = client.call("operation.get", {"operation_id": operation_id}, **(context_guards or {}))
        if not current.get("ok"):
            current.setdefault("error", {}).setdefault("details", {})["operation_id"] = operation_id
            return current
        state = current["data"].get("state")
        if state == "succeeded":
            return {
                **current,
                "data": {
                    "operation_id": operation_id,
                    "state": state,
                    "result": current["data"].get("result"),
                },
            }
        if state in {"failed", "cancelled", "uncertain"}:
            error = current["data"].get("error") or {}
            result = error_envelope(
                "RECONCILIATION_REQUIRED"
                if state == "uncertain"
                else error.get("code", "OPERATION_FAILED"),
                error.get("message", f"Operation reached {state}"),
                request_id=current.get("request_id"),
                details={
                    "operation_id": operation_id,
                    "state": state,
                    "result": current["data"].get("result"),
                },
                next_action="Inspect this operation before any retry",
            )
            for field in ("action_digest", "next_context"):
                if field in current:
                    result[field] = current[field]
            return result
        remaining = deadline - clock()
        if remaining <= 0:
            return error_envelope(
                "RECONCILIATION_REQUIRED",
                "Wait expired; the retained operation is still pending",
                request_id=current.get("request_id"),
                details={"operation_id": operation_id, "state": state},
                next_action="Inspect operation get with the retained operation ID; do not submit another operation",
            )
        sleep(min(delay, remaining))
        delay = min(delay * 1.5, 0.5)


def _validate_wait_timeout(timeout):
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise CoordinationError(
            "INVALID_ARGUMENT", "Wait timeout must be finite and between zero and one day"
        )


def prepare_patch_file(workspace, filename):
    filename = Path(filename)
    candidate = filename if filename.is_absolute() else workspace.root / filename
    try:
        relative = candidate.relative_to(workspace.root)
    except ValueError as exc:
        raise CoordinationError(
            "INVALID_ARGUMENT", "Patch artifact must be inside the selected workspace"
        ) from exc
    current = workspace.root
    for part in relative.parts:
        if part in {".", ".."}:
            raise CoordinationError("INVALID_ARGUMENT", "Patch artifact path must be normalized")
        current /= part
        if current.is_symlink():
            raise CoordinationError("INVALID_ARGUMENT", "Patch artifact must not contain symlinks")
    if not candidate.is_file() or candidate.stat().st_size > 16 * 1024 * 1024:
        raise CoordinationError(
            "INVALID_ARGUMENT", "Patch artifact must be a regular file no larger than 16 MiB"
        )
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        total = 0
        while chunk := stream.read(65536):
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise CoordinationError("INVALID_ARGUMENT", "Patch artifact grew beyond 16 MiB")
            digest.update(chunk)
    return {"patch_file": relative.as_posix(), "patch_sha256": digest.hexdigest()}


def _read_text_file(path):
    with path.open("rb") as stream:
        data = stream.read(262145)
    if len(data) > 262144:
        raise CoordinationError("INVALID_ARGUMENT", "Literal input file exceeds 256 KiB")
    return data.decode("utf-8", errors="strict")


def _maintenance(args, workspace):
    from . import application

    if args.maintenance == "serve":
        application.run_service(workspace)
        return None
    if args.maintenance == "mcp":
        from .mcp import run

        run(workspace, harness=args.harness)
        return None
    if args.maintenance == "init":
        from .install import generate_candidates, init_project

        root = args.root or Path.cwd()
        if args.candidate_dir:
            return generate_candidates(root, args.candidate_dir)
        result = init_project(root, apply=args.apply)
        if args.apply:
            from .config import register_workspace

            registered = register_workspace(root)
            result["workspace_id"] = registered.id
        return result
    if args.maintenance == "hook":
        return hook(workspace, args.harness, args.event)
    if args.maintenance in {"backup", "restore"}:
        function = (
            application.backup_workspace
            if args.maintenance == "backup"
            else application.restore_workspace
        )
        return function(workspace, args.path)
    if args.maintenance == "migrate":
        from . import migrate

        if args.migrate_action == "inspect":
            manifest = migrate.inspect_sources(migrate.load_source_specs(args.sources))
            migrate.save_manifest(manifest, args.output_manifest)
            return {**manifest.summary(), "manifest_path": str(args.output_manifest)}
        manifest = migrate.load_manifest(args.manifest)
        with application.open_offline_store(workspace) as store:
            if args.migrate_action == "import":
                report = migrate.apply_import(
                    store, migrate.prepare_import(manifest, workspace), run_id=args.run_id
                )
                if report["state"] != "complete":
                    raise CoordinationError(
                        "MIGRATION_BLOCKED", "Migration did not complete", details=report
                    )
                return report
            report = migrate.verify_import(store, manifest)
            if not report["valid"]:
                raise CoordinationError(
                    "MIGRATION_VERIFICATION_FAILED", "Migration verification failed", details=report
                )
            return report
    client = Client(
        workspace.socket_path, workspace_id=workspace.id, operator=True, transport="operator"
    )
    try:
        if args.maintenance == "monitor":
            from .monitor import run

            exit_code = run(client)
            if exit_code != 0:
                raise CoordinationError(
                    "MONITOR_UNAVAILABLE", "Cannot open coordination monitor",
                    details={"exit_code": exit_code},
                )
            return None
        if args.maintenance == "doctor":
            from .doctor import inspect

            return inspect(workspace, client=client, live=args.live)
        if args.maintenance == "service":
            from . import install

            if args.service_action in {"health", "drain", "activate"}:
                return getattr(client, args.service_action)()
            if args.service_action == "status":
                return install.service_status(workspace, Path.home(), "agentcoord")
            if args.service_action == "upgrade":
                return install.install_service(
                    workspace,
                    Path.home(),
                    "agentcoord",
                    apply=args.apply,
                    restart=True,
                    client=client,
                )
            function = (
                install.install_service
                if args.service_action == "install"
                else install.remove_service
            )
            return function(workspace, Path.home(), "agentcoord", apply=args.apply, client=client)
        raise CoordinationError("INVALID_ARGUMENT", "Unknown maintenance command")
    finally:
        client.close()


def hook(workspace, harness, event, *, payload=None, client_factory=Client):
    """Convert one native lifecycle payload without inventing its originating run."""
    from .identity import native_context as capture_native

    if payload is None:
        raw = sys.stdin.buffer.read(262145)
        if len(raw) > 262144:
            raise CoordinationError("INVALID_ARGUMENT", "Lifecycle payload exceeds 256 KiB")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeError) as exc:
            raise CoordinationError(
                "INVALID_ARGUMENT", "Lifecycle payload must be UTF-8 JSON"
            ) from exc
    if not isinstance(payload, dict):
        raise CoordinationError("INVALID_ARGUMENT", "Lifecycle payload must be an object")
    from .config import discover_workspace, load_config

    observed_roots = [
        payload[key] for key in ("cwd", "workspace_root", "project_dir") if key in payload
    ]
    reason = "lifecycle payload has no valid workspace path"
    for observed_root in observed_roots:
        if (
            not isinstance(observed_root, str)
            or not observed_root.strip()
            or not Path(observed_root).is_absolute()
        ):
            break
        try:
            observed = discover_workspace(
                Path(observed_root), state_root=workspace.state_root, use_environment=False
            )
        except (ValueError, OSError, RuntimeError):
            break
        except CoordinationError as error:
            if error.code not in {"NOT_FOUND", "WRONG_WORKSPACE"}:
                raise
            reason = "event belongs to another workspace"
            break
        if observed.id != workspace.id:
            reason = "event belongs to another workspace"
            break
    else:
        if observed_roots:
            reason = None
    if reason is not None:
        return {
            "ok": True,
            "protocol": 1,
            "request_id": None,
            "data": {"applied": False, "reason": reason},
            "action_digest": None,
        }

    evidence = capture_native(
        harness, payload=payload, executable_paths=load_config(workspace).native_executables
    )
    if (
        event == "child_start"
        and evidence.get("child_id")
        and os.environ.get("AGENTCOORD_PARENT_ID")
    ):
        evidence.update(parent_id=os.environ["AGENTCOORD_PARENT_ID"], source="native_child_start")
    state = {
        "start": "working",
        "child_start": "working",
        "stop": "idle",
        "child_stop": "completed",
        "end": "idle",
        "failure": "blocked",
    }[event]
    if (
        payload.get("is_interrupt") is True
        or payload.get("status") == "aborted"
        or payload.get("reason") == "aborted"
    ):
        state = "paused"
    arguments = {"event": event, "state": state}
    if evidence.get("native_run_id"):
        arguments["native_run_id"] = evidence["native_run_id"]
    # A current binding's generation is deliberately absent from this decision.
    generation = payload.get("execution_generation") or payload.get("lifecycle_generation")
    if generation is not None:
        arguments["execution_generation"] = generation
    key = payload.get("event_id") or secrets.token_hex(16)
    with client_factory(
        workspace.socket_path, evidence, workspace_id=workspace.id, transport="hook"
    ) as client:
        return client.call("identity.event", arguments, key=key)


def _origin_context(raw):
    if raw is None:
        return None
    try:
        origin = json.loads(raw)
    except ValueError as error:
        raise CoordinationError("INVALID_ARGUMENT", "Origin context must be a JSON object") from error
    fields = {"workspace_id", "actor_id", "task_generation", "execution_generation"}
    validate_fields(origin, fields, fields)
    return {name: identifier(value, name) for name, value in origin.items()}


def _origin_guards(client, origin):
    if origin is None:
        return {}
    reply = client.call("identity.get")
    if not reply.get("ok"):
        error = reply.get("error", {})
        raise CoordinationError(error.get("code", "UNBOUND_ACTOR"), error.get("message", "Native identity unavailable"))
    data = reply["data"]
    actor = data["actor"]
    if data.get("workspace_id") != origin["workspace_id"]:
        raise CoordinationError("WRONG_WORKSPACE", "Origin context belongs to another workspace")
    if actor["id"] != origin["actor_id"]:
        raise CoordinationError("NOT_AUTHORIZED", "Origin context belongs to another native actor")
    if (actor["current_task_generation"] != origin["task_generation"]
            or actor["current_execution_generation"] != origin["execution_generation"]):
        raise CoordinationError("STALE_GENERATION", "Originating task or native execution changed")
    return {"expected_task_generation": origin["task_generation"],
            "expected_execution_generation": origin["execution_generation"]}


def _write_hook_error(error):
    diagnostic = f"agentcoord hook failed: {error['code']}: {error['message']}"
    sys.stderr.write(diagnostic[:4096] + "\n")


def main(argv=None, *, client_factory=None):
    args = parser().parse_args(argv)
    try:
        from .config import discover_workspace

        origin = _origin_context(args.origin_context)
        if origin is not None and (args.operator or getattr(args, "maintenance", None)):
            raise CoordinationError("INVALID_ARGUMENT", "Origin context requires a native actor operation")
        if getattr(args, "maintenance", None) == "init":
            result = _maintenance(args, None)
            workspace = None
        else:
            workspace = discover_workspace(explicit_root=args.root)
        if origin is not None and workspace.id != origin["workspace_id"]:
            raise CoordinationError("WRONG_WORKSPACE", "Origin context belongs to another workspace")
        if getattr(args, "maintenance", None) and args.maintenance != "init":
            result = _maintenance(args, workspace)
        elif not getattr(args, "maintenance", None):
            if hasattr(args, "wait_timeout"):
                _validate_wait_timeout(args.wait_timeout)
            arguments = {
                name: getattr(args, name)
                for name in args.spec.schema()["properties"]
                if hasattr(args, name)
            }
            if hasattr(args, "evidence_json"):
                if "evidence" in arguments:
                    raise CoordinationError(
                        "INVALID_ARGUMENT", "Choose --evidence or --evidence-json"
                    )
                arguments["evidence"] = args.evidence_json
            for field in ("body", "message"):
                file = getattr(args, field + "_file", None)
                if file is not None:
                    if field in arguments:
                        raise CoordinationError(
                            "INVALID_ARGUMENT", f"Choose --{field} or --{field}-file"
                        )
                    arguments[field] = _read_text_file(file)
            if args.spec.operation == "commit.execute" and arguments.get("patch_file"):
                artifact = prepare_patch_file(workspace, arguments["patch_file"])
                if (
                    "patch_sha256" in arguments
                    and arguments["patch_sha256"] != artifact["patch_sha256"]
                ):
                    raise CoordinationError(
                        "INVALID_ARGUMENT", "Patch artifact does not match the supplied hash"
                    )
                arguments.update(artifact)
            factory = client_factory or Client
            from .config import load_config

            context = (
                {}
                if args.operator
                else native_context(
                    args.harness, executable_paths=load_config(workspace).native_executables
                )
            )
            with factory(
                workspace.socket_path,
                context,
                workspace_id=workspace.id,
                operator=args.operator,
                transport="operator" if args.operator else "cli",
            ) as client:
                guards = _origin_guards(client, origin)
                result = invoke(client, args.spec, arguments, context_guards=guards)
                if hasattr(args, "no_wait") and not args.no_wait:
                    result = wait_operation(client, result, timeout=args.wait_timeout,
                                            context_guards=guards)
        if getattr(args, "maintenance", None) == "hook":
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise CoordinationError("INVALID_RESPONSE", "Lifecycle hook returned no valid result")
            if result["ok"]:
                if args.harness == "cursor":
                    sys.stdout.write("{}\n")
                return 0
            _write_hook_error(result["error"])
            return 1
        if result is None:
            return 0
        if "ok" not in result:
            result = {
                "ok": True,
                "protocol": 1,
                "request_id": None,
                "data": result,
                "action_digest": None,
            }
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
        return 0 if result.get("ok") else 1
    except (CoordinationError, OSError, ValueError, RuntimeError) as exc:
        result = (
            error_envelope(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                details=exc.details,
                next_action=exc.next_action,
            )
            if isinstance(exc, CoordinationError)
            else error_envelope("OPERATION_FAILED", str(exc))
        )
        if getattr(args, "maintenance", None) == "hook":
            _write_hook_error(result["error"])
        else:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 1
