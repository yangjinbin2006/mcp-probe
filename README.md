# mcp-probe

A health check for Model Context Protocol servers that can run in CI.

The usual answer to a broken MCP server is "open the Inspector". That works at a desk. It
does not work in a build, it does not tell you which layer broke, and it will show you a
connected server with zero tools without explaining why.

mcp-probe starts the server, does the handshake, lists the tools, checks their schemas,
optionally calls one, then reports the first layer that broke and the reason.

Standard library only. Python 3.8+.

```bash
python mcp_probe.py --command "python examples/echo_server.py"
```

```
mcp-probe 1.0.0
====================================================================
command   python examples/echo_server.py
server    echo-server (protocol 2025-06-18)
--------------------------------------------------------------------
[ OK ] tools      2 tool(s) exposed
[ OK ] call       tools/call 'echo' returned 1 content block(s)
--------------------------------------------------------------------
healthy -- 0 failure(s), 0 warning(s), 2 tool(s)
```

## The case it was written for

For a stdio server, stdout is the protocol channel. One stray debug print corrupts the
message stream, and what the client reports never mentions the real cause:

- "connection failed"
- "server connected but no tools"
- "transport error"

mcp-probe reports the line itself:

```
[FAIL] wire       1 non-JSON line(s) on stdout
       stdout is the protocol channel for stdio servers. Anything
       else written there corrupts the message stream.
       fix: Move debug output to stderr, or route it into the MCP
            logging capability. First offending line: 'server ready'
```

## What it checks

| Area | Finds |
|---|---|
| `launch` | Executable missing, process exits during startup, non-zero exit after answering |
| `handshake` | `initialize` timeout, handshake error, no `tools` capability |
| `tools` | Empty `tools/list`, duplicate names, missing descriptions, invalid names (length, charset) |
| `schema` | `inputSchema` missing, not `object`, `required` naming absent properties, properties without a type |
| `call` | `tools/call` error, `isError` result, timeout (via `--call`) |
| `wire` | Non-JSON lines on stdout |

## Usage

```bash
# a command line
python mcp_probe.py --command "npx -y @modelcontextprotocol/server-filesystem /tmp"

# a server from an existing MCP config
python mcp_probe.py --config ./mcp.json --server filesystem

# also call one tool
python mcp_probe.py --command "python server.py" --call search --args '{"q":"test"}'

# pass environment through
python mcp_probe.py --command "node build/index.js" --env API_KEY=... --cwd ./server
```

| Flag | Meaning |
|---|---|
| `--command` | Server command line, shell-style quoting supported |
| `--config` + `--server` | Read `command` / `args` / `env` / `cwd` from an MCP config file |
| `--cwd` | Working directory for the server process |
| `--env KEY=VALUE` | Extra environment variable, repeatable |
| `--timeout` | Per-request timeout, default 20s |
| `--call` / `--args` | Call a tool once with a JSON argument object |
| `--json` | Machine-readable report |
| `--fail-level` | `fail` (default), `warn`, or `never` |

## In CI

```bash
python mcp_probe.py --config ./mcp.json --server filesystem --json
```

Exit codes: `0` healthy, `1` findings at or above `--fail-level`, `2` usage error.

Because it asserts instead of prompting, it catches a protocol regression as soon as a
dependency bump changes what the server puts on the wire:

```yaml
- name: probe MCP server
  run: python mcp_probe.py --command "node dist/index.js" --call search --args '{"q":"x"}'
```

## Design notes

- No SDK. The point is to see the wire, including the parts an SDK normalises away. The
  client is about 150 lines and hand-rolled for that reason.
- One finding, not a list of symptoms. A failed handshake makes the tool list meaningless,
  and printing both buries the cause.
- Timeouts count as failures. A server that never answers is broken, and CI has nobody to
  wait for it.
- The probe only reads. It starts the process, asks questions, and kills it on the way out.
  Your config is never touched.

## Limits

- stdio transport only. Streamable HTTP servers need a different probe, since this one
  spawns a process.
- No protocol negotiation. It speaks `2025-06-18`. If the handshake is rejected, the
  finding says so rather than retrying an older revision.
- Schema checks are structural. They validate shape, not whether the schema describes what
  the tool actually does.

## License

MIT
