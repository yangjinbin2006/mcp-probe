#!/usr/bin/env python3
"""mcp-probe — a CI-friendly health check for Model Context Protocol servers.

The usual advice for a misbehaving MCP server is "open the Inspector". That works
while you are sitting at a desk. It does not work in CI, and it does not tell you
which of the six things went wrong.

`mcp-probe` starts a stdio server, does the handshake, lists the tools, validates
their schemas, and optionally calls one. It reports the first broken layer, so you
fix the actual cause instead of reinstalling packages.

The single most common MCP failure it catches: debug logging written to stdout.
stdout is the protocol channel for stdio servers, so one stray `print("ready")`
corrupts the stream and the client reports anything from "connection failed" to
"no tools" — never "you printed to the wrong stream".

Zero third-party dependencies. Python 3.8+.

    python mcp_probe.py --command "python examples/echo_server.py"
    python mcp_probe.py --config ./mcp.json --server filesystem
    python mcp_probe.py --command "npx -y some-server" --call echo --args '{"text":"hi"}'

Exit codes: 0 healthy, 1 findings at or above --fail-level, 2 usage error.

License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

SEVERITY_WEIGHT = {FAIL: 15, WARN: 5, INFO: 0}


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    level: str
    area: str
    code: str
    message: str
    cause: str = ""
    fix: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    command: str = ""
    findings: List[Finding] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    protocol_version: str = ""
    server_name: str = ""
    stderr_sample: str = ""

    def add(self, level: str, area: str, code: str, message: str, cause: str = "", fix: str = "") -> None:
        self.findings.append(Finding(level, area, code, message, cause, fix))

    @property
    def failures(self) -> int:
        return sum(1 for f in self.findings if f.level == FAIL)


# --------------------------------------------------------------------------
# stdio client
# --------------------------------------------------------------------------


class StdoutCorruption(Exception):
    """Raised when the server writes something to stdout that is not JSON-RPC."""


class McpStdioClient:
    """Minimal JSON-RPC client for newline-delimited stdio transport.

    Deliberately not using the official SDK: the point is to observe the wire,
    including the parts an SDK would swallow or normalise away.
    """

    def __init__(self, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.proc: Optional[subprocess.Popen] = None
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.raw_non_json: List[str] = []
        self.stderr_lines: List[str] = []
        self._next_id = 0
        self._closed = False

    def start(self, timeout: float = 20.0) -> None:
        argv = shlex.split(self.command, posix=False) if isinstance(self.command, str) else self.command
        argv = [a.strip('"') for a in argv]
        if not argv:
            raise ValueError("empty command")

        env = dict(os.environ)
        if self.env:
            env.update(self.env)

        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = json.loads(stripped)
            except ValueError:
                # Anything that is not JSON on stdout is a protocol violation.
                self.raw_non_json.append(stripped)
                continue
            if isinstance(message, dict):
                self.inbox.put(message)

    def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            if len(self.stderr_lines) < 200:
                self.stderr_lines.append(line.rstrip())

    def _send(self, payload: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 20.0) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no response to %s within %.0fs" % (method, timeout))
            try:
                message = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("no response to %s within %.0fs" % (method, timeout))
            if message.get("id") == request_id:
                return message
            # A notification or a different reply: ignore and keep waiting.

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        if self._closed or not self.proc:
            return
        self._closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------

JSON_TYPES = {"string", "number", "integer", "boolean", "object", "array", "null"}


def validate_input_schema(schema: Any) -> List[str]:
    """Return a list of problems. Empty means the schema is usable."""
    problems: List[str] = []
    if schema is None:
        return ["missing inputSchema"]
    if not isinstance(schema, dict):
        return ["inputSchema is %s, expected an object" % type(schema).__name__]

    schema_type = schema.get("type")
    if schema_type is None:
        problems.append("inputSchema has no 'type'")
    elif schema_type != "object":
        problems.append("inputSchema type is %r, MCP tool arguments must be an object" % schema_type)

    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        problems.append("'properties' is not an object")
        properties = {}

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            problems.append("'required' is not an array")
        else:
            for name in required:
                if not isinstance(name, str):
                    problems.append("'required' contains a non-string entry")
                elif properties is not None and name not in properties:
                    problems.append("'required' names %r which is not in 'properties'" % name)

    for name, subschema in (properties or {}).items():
        if not isinstance(subschema, dict):
            problems.append("property %r is not an object" % name)
            continue
        declared = subschema.get("type")
        if declared is None:
            if not any(k in subschema for k in ("enum", "anyOf", "oneOf", "allOf", "$ref")):
                problems.append("property %r has no type and no enum/anyOf/$ref" % name)
        elif isinstance(declared, str):
            if declared not in JSON_TYPES:
                problems.append("property %r has unknown type %r" % (name, declared))
        elif isinstance(declared, list):
            for entry in declared:
                if entry not in JSON_TYPES:
                    problems.append("property %r has unknown type %r" % (name, entry))
        else:
            problems.append("property %r has a malformed type" % name)

    return problems


def validate_tool_name(name: str) -> Optional[str]:
    if not name:
        return "empty name"
    if len(name) > 64:
        return "name is %d characters, over the 64 limit" % len(name)
    bad = [c for c in name if not (c.isalnum() or c in "-_")]
    if bad:
        return "name contains %s; use only letters, digits, underscore and hyphen" % ", ".join(repr(c) for c in sorted(set(bad))[:3])
    return None


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------


def probe(
    command: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    call_tool: Optional[str] = None,
    call_args: Optional[dict] = None,
) -> Report:
    report = Report(command=command)
    client = McpStdioClient(command, cwd=cwd, env=env)

    try:
        try:
            client.start(timeout)
        except FileNotFoundError:
            report.add(FAIL, "launch", "MP001", "executable not found",
                       cause="The first token of the command is not on PATH for this process.",
                       fix="Use an absolute path, or check that the runtime (npx, uv, python) is installed.")
            return report
        except Exception as exc:
            report.add(FAIL, "launch", "MP001", "could not start process: %s" % exc)
            return report

        # --- handshake -----------------------------------------------------
        try:
            response = client.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-probe", "version": VERSION},
                },
                timeout=timeout,
            )
        except TimeoutError:
            _diagnose_launch_failure(report, client, timeout)
            return report

        if "error" in response:
            error = response["error"]
            report.add(FAIL, "handshake", "MP002",
                       "initialize returned an error: %s" % error.get("message", error),
                       cause="The server rejected the handshake.",
                       fix="Check the protocol version the server supports; a breaking revision landed in 2026-07.")
            return report

        result = response.get("result") or {}
        report.protocol_version = str(result.get("protocolVersion", "?"))
        server_info = result.get("serverInfo") or {}
        report.server_name = str(server_info.get("name", ""))
        capabilities = result.get("capabilities") or {}

        if not capabilities.get("tools"):
            report.add(WARN, "handshake", "MP003", "server declares no 'tools' capability",
                       cause="It may be a resources-only or prompts-only server.",
                       fix="Confirm that is intentional; tools/list will be empty by design.")

        client.notify("notifications/initialized")

        # --- tools/list ----------------------------------------------------
        try:
            listing = client.request("tools/list", {}, timeout=timeout)
        except TimeoutError:
            report.add(FAIL, "tools", "MP004", "tools/list timed out",
                       cause="The server accepted the handshake but does not answer tool discovery.",
                       fix="Implement tools/list, or check for a deadlock in the request handler.")
            return report

        if "error" in listing:
            error = listing["error"]
            report.add(FAIL, "tools", "MP005", "tools/list returned an error: %s" % error.get("message", error))
            return report

        tools = (listing.get("result") or {}).get("tools") or []
        if not tools:
            report.add(FAIL, "tools", "MP006", "server exposes zero tools",
                       cause="Registration code did not run, an import failed silently, or a filter "
                             "returned an empty list.",
                       fix="Log registration to stderr and confirm the tools are added before the "
                           "transport starts serving.")
        else:
            report.add(OK, "tools", "MP000", "%d tool(s) exposed" % len(tools))

        seen: Dict[str, int] = {}
        for tool in tools:
            if not isinstance(tool, dict):
                report.add(FAIL, "tools", "MP007", "a tool entry is not an object")
                continue
            name = str(tool.get("name", ""))
            report.tools.append(name)

            problem = validate_tool_name(name)
            if problem:
                report.add(FAIL, "tools", "MP008", "tool %r: %s" % (name or "<unnamed>", problem))

            if name in seen:
                report.add(FAIL, "tools", "MP009", "duplicate tool name %r" % name,
                           cause="Two registrations share a name; which one wins is unspecified.")
            seen[name] = seen.get(name, 0) + 1

            description = str(tool.get("description", "")).strip()
            if not description:
                report.add(WARN, "tools", "MP010", "tool %r has no description" % name,
                           cause="The model picks tools by description. Without one it guesses from the name.",
                           fix="Say what the tool does and when to use it, in one or two sentences.")

            schema_problems = validate_input_schema(tool.get("inputSchema"))
            for problem in schema_problems:
                report.add(FAIL, "schema", "MP011", "tool %r: %s" % (name, problem),
                           cause="Clients build argument forms and validators from this schema.",
                           fix="Fix the schema; an invalid one is often dropped silently by the client.")

        # --- optional smoke call -------------------------------------------
        if call_tool:
            try:
                call = client.request("tools/call", {"name": call_tool, "arguments": call_args or {}}, timeout=timeout)
                if "error" in call:
                    error = call["error"]
                    report.add(FAIL, "call", "MP012",
                               "tools/call %r failed: %s" % (call_tool, error.get("message", error)))
                else:
                    payload = call.get("result") or {}
                    if payload.get("isError"):
                        report.add(FAIL, "call", "MP013",
                                   "tools/call %r returned isError" % call_tool,
                                   cause="The tool ran and reported failure.",
                                   fix="Inspect the content blocks in the response for the actual error.")
                    else:
                        blocks = payload.get("content") or []
                        report.add(OK, "call", "MP000",
                                   "tools/call %r returned %d content block(s)" % (call_tool, len(blocks)))
            except TimeoutError:
                report.add(FAIL, "call", "MP014", "tools/call %r timed out" % call_tool)

        # --- wire hygiene ---------------------------------------------------
        if client.raw_non_json:
            sample = client.raw_non_json[0][:120]
            report.add(FAIL, "wire", "MP015",
                       "%d non-JSON line(s) on stdout" % len(client.raw_non_json),
                       cause="stdout is the protocol channel for stdio servers. Anything else written "
                             "there corrupts the message stream.",
                       fix="Move debug output to stderr, or route it into the MCP logging capability. "
                           "First offending line: %r" % sample)
        if client.stderr_lines:
            report.stderr_sample = "\n".join(client.stderr_lines[:8])

    finally:
        client.close()

    # The process dying right after the last reply is worth reporting.
    if client.proc is not None and client.proc.poll() not in (None, 0) and not report.failures:
        report.add(WARN, "launch", "MP016",
                   "server exited with code %s" % client.proc.returncode,
                   cause="It answered, then crashed. Fine for a one-shot probe, suspicious for a daemon.")

    return report


def _diagnose_launch_failure(report: Report, client: McpStdioClient, timeout: float) -> None:
    """Explain a handshake timeout using whatever the process actually did."""
    if client.raw_non_json:
        report.add(FAIL, "wire", "MP015",
                   "handshake timed out, and the server wrote %d non-JSON line(s) to stdout"
                   % len(client.raw_non_json),
                   cause="A stray debug print on stdout corrupted the message stream, so the client "
                         "never saw the handshake reply.",
                   fix="Send diagnostics to stderr. First offending line: %r"
                       % client.raw_non_json[0][:120])
        return

    if not client.alive():
        code = client.proc.returncode if client.proc else "?"
        tail = "\n".join(client.stderr_lines[-6:]) or "(no stderr)"
        report.add(FAIL, "launch", "MP001", "process exited with code %s before completing the handshake" % code,
                   cause="The server crashed on startup.",
                   fix="Run the command directly in a terminal and read the traceback.\n" + tail)
        return

    tail = "\n".join(client.stderr_lines[-6:]) or "(no stderr)"
    report.add(FAIL, "handshake", "MP017", "no reply to initialize within %.0fs" % timeout,
               cause="The process is alive but silent: it may be waiting for input, blocked on network, "
                     "or writing the reply somewhere other than stdout.",
               fix="Re-run with a longer --timeout. If it stays silent, test the server with the official "
                   "Inspector to compare.\n" + tail)


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------


def load_from_config(config_path: Path, server_name: str) -> Optional[dict]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("Could not read %s: %s" % (config_path, exc), file=sys.stderr)
        return None

    servers = data.get("mcpServers") or data.get("servers") or {}
    entry = servers.get(server_name)
    if entry is None:
        available = ", ".join(sorted(servers)) or "(none)"
        print("No server named %r. Available: %s" % (server_name, available), file=sys.stderr)
        return None
    return entry


def command_from_entry(entry: dict) -> str:
    command = entry.get("command", "")
    args = entry.get("args") or []
    parts = [command] + [str(a) for a in args]
    return " ".join(shlex.quote(p) if " " in p else p for p in parts)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

MARKS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}


def render(report: Report) -> str:
    out: List[str] = ["mcp-probe %s" % VERSION, "=" * 68]
    out.append("command   %s" % report.command)
    if report.server_name or report.protocol_version:
        out.append("server    %s (protocol %s)" % (report.server_name or "?", report.protocol_version or "?"))
    out.append("-" * 68)

    for finding in report.findings:
        out.append("%s %-10s %s" % (MARKS.get(finding.level, "[????]"), finding.area, finding.message))
        if finding.cause:
            for line in wrap(finding.cause, 60):
                out.append("       " + line)
        if finding.fix:
            for i, line in enumerate(finding.fix.splitlines()):
                for j, wrapped in enumerate(wrap(line, 58)):
                    prefix = "       fix: " if (i == 0 and j == 0) else "            "
                    out.append(prefix + wrapped)

    out.append("-" * 68)
    verdict = "healthy" if not report.failures else "not healthy"
    out.append("%s -- %d failure(s), %d warning(s), %d tool(s)"
               % (verdict, report.failures,
                  sum(1 for f in report.findings if f.level == WARN),
                  len(report.tools)))
    return "\n".join(out)


def wrap(text: str, width: int) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-probe",
        description="Start an MCP stdio server, handshake, list tools, validate schemas, optionally call one.",
    )
    parser.add_argument("--command", help="server command line, e.g. \"python server.py\"")
    parser.add_argument("--config", help="MCP config JSON to read the server definition from")
    parser.add_argument("--server", help="server name inside --config")
    parser.add_argument("--cwd", help="working directory for the server process")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra environment variable (repeatable)")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request timeout in seconds")
    parser.add_argument("--call", metavar="TOOL", help="also call this tool once")
    parser.add_argument("--args", metavar="JSON", default="{}", help="arguments for --call, as JSON")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--fail-level", choices=["fail", "warn", "never"], default="fail")
    parser.add_argument("--version", action="version", version="mcp-probe %s" % VERSION)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_parser().parse_args(argv)

    env: Dict[str, str] = {}
    for item in args.env:
        if "=" not in item:
            print("--env expects KEY=VALUE, got %r" % item, file=sys.stderr)
            return 2
        key, _, value = item.partition("=")
        env[key] = value

    command = args.command
    cwd = args.cwd

    if args.config:
        if not args.server:
            print("--config requires --server", file=sys.stderr)
            return 2
        entry = load_from_config(Path(args.config).expanduser(), args.server)
        if entry is None:
            return 2
        command = command or command_from_entry(entry)
        cwd = cwd or entry.get("cwd")
        for key, value in (entry.get("env") or {}).items():
            env.setdefault(key, str(value))

    if not command:
        print("Provide --command, or --config with --server", file=sys.stderr)
        return 2

    try:
        call_args = json.loads(args.args) if args.call else None
    except ValueError as exc:
        print("--args is not valid JSON: %s" % exc, file=sys.stderr)
        return 2
    if call_args is not None and not isinstance(call_args, dict):
        print("--args must be a JSON object", file=sys.stderr)
        return 2

    report = probe(
        command=command,
        cwd=cwd,
        env=env or None,
        timeout=args.timeout,
        call_tool=args.call,
        call_args=call_args,
    )

    if args.json:
        print(json.dumps(
            {
                "version": VERSION,
                "command": report.command,
                "server": report.server_name,
                "protocol": report.protocol_version,
                "tools": report.tools,
                "healthy": report.failures == 0,
                "findings": [f.__dict__ for f in report.findings],
            },
            indent=2,
            ensure_ascii=False,
        ))
    else:
        print(render(report))

    if args.fail_level == "never":
        return 0
    if args.fail_level == "warn":
        return 1 if any(f.level in (FAIL, WARN) for f in report.findings) else 0
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
