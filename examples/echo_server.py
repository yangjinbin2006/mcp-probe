#!/usr/bin/env python3
"""A minimal, correct MCP stdio server — used as a probe fixture and as a template.

Two tools: `echo` (returns what you send) and `add` (sums two numbers).

Deliberately readable: no SDK, no framework. The whole protocol surface a stdio
server needs is initialize, notifications/initialized, tools/list and tools/call.

Note what this file does NOT do: print to stdout. stdout is the protocol channel,
so every diagnostic goes to stderr. That single rule is the cause of most MCP
"connected but no tools" reports.
"""

import json
import sys

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the given text. Use when the user asks to verify that the "
                       "server is reachable, or to round-trip a string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo back"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum. Use for arithmetic on exactly two operands.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First addend"},
                "b": {"type": "number", "description": "Second addend"},
            },
            "required": ["a", "b"],
        },
    },
]


def log(message: str) -> None:
    """stderr only. Never stdout."""
    sys.stderr.write("[echo-server] %s\n" % message)
    sys.stderr.flush()


def reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(request: dict) -> None:
    method = request.get("method", "")
    request_id = request.get("id")

    # A notification has no id and must not be answered.
    if request_id is None:
        log("notification: %s" % method)
        return

    if method == "initialize":
        reply({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "echo-server", "version": "1.0.0"},
            },
        })
        return

    if method == "tools/list":
        reply({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name == "echo":
                text = arguments.get("text", "")
                reply({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": str(text)}], "isError": False},
                })
                return
            if name == "add":
                total = float(arguments.get("a", 0)) + float(arguments.get("b", 0))
                reply({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": str(total)}], "isError": False},
                })
                return
            reply({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "unknown tool: %s" % name},
            })
        except (TypeError, ValueError) as exc:
            reply({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": "bad arguments: %s" % exc}], "isError": True},
            })
        return

    reply({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "method not found: %s" % method},
    })


def main() -> int:
    log("started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            log("ignoring non-JSON input")
            continue
        if isinstance(request, dict):
            handle(request)
    log("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
