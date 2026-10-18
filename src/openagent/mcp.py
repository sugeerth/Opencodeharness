"""Open Agent as an MCP server: `openagent mcp`.

Any MCP client (Claude Code, Claude Desktop, Cursor, OpenCode itself) can hand a coding task to
OpenCode and get back a verified answer: the check passed or it did not, what changed, and the
check's output when it failed. Stdio transport, JSON-RPC 2.0, one request at a time; the standard
library only.

  claude mcp add openagent -- openagent mcp -m anthropic/claude-sonnet-5-5

Tools:
  solve        run OpenCode on a task in a directory, verifying and repairing until a check passes
  check        run a shell command in a directory and report whether it passes (no agent)
  presets      the strategies `solve` can use
"""
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable

from . import __version__
from .agent import PRESETS, solve, with_overrides
from .opencode import OpenCode
from .summary import markdown
from .verify import excerpt, run_check

PROTOCOL = "2025-06-18"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")


class MethodNotFound(Exception):
    pass


class InvalidParams(Exception):
    pass

TOOLS = [
    {"name": "solve",
     "description": "Have OpenCode make a change in a repository, then run a check command (e.g. the tests) and, "
                    "while it fails, show OpenCode the failure and let it repair its work, until the check "
                    "passes or the rounds run out. Returns whether the check passed, what changed, and a summary.",
     "inputSchema": {"type": "object", "required": ["task", "directory"], "properties": {
         "task": {"type": "string", "description": "what to do, in plain words"},
         "directory": {"type": "string", "description": "absolute path of the repository to work in"},
         "check": {"type": "string", "description": "shell command that exits 0 when the task is done, e.g. 'pytest -q'"},
         "protect": {"type": "array", "items": {"type": "string"},
                     "description": "globs the agent may not change, e.g. ['tests/**']"},
         "preset": {"type": "string", "enum": list(PRESETS), "default": "openagent"},
         "rounds": {"type": "integer", "minimum": 0, "description": "repair rounds after the first try"},
         "model": {"type": "string", "description": "OpenCode provider/model; defaults to the server's"},
         "max_cost": {"type": "number", "description": "stop once the runs have cost this many US dollars"}}}},
    {"name": "check",
     "description": "Run a shell command in a directory and report whether it exits 0, with the parts of its "
                    "output that explain a failure.",
     "inputSchema": {"type": "object", "required": ["command", "directory"], "properties": {
         "command": {"type": "string"}, "directory": {"type": "string"},
         "timeout": {"type": "integer", "default": 600}}}},
    {"name": "presets", "description": "The strategies `solve` can use, and what each does.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Server:
    def __init__(self, oc: OpenCode, log: Callable[[str], None] = lambda _: None):
        self.oc = oc
        self.log = log

    def handle(self, msg: dict) -> dict | None:
        """One JSON-RPC message in; the response out (None for a notification)."""
        if not isinstance(msg, dict):  # a batch or a bare value: not a request this server takes
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        method, mid = msg.get("method"), msg.get("id")
        if mid is None or "method" not in msg:  # notifications, and responses: nothing to answer
            return None
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object"}}
        try:
            result = self.dispatch(method, params)
        except MethodNotFound:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method!r}"}}
        except InvalidParams as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
        except Exception as e:  # the server keeps going
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in SUPPORTED else PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "openagent", "version": __version__},
                    "instructions": "Use `solve` to have OpenCode make a change that a check command verifies."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                raise InvalidParams(f"unknown tool {name!r}")
            if not isinstance(args, dict):
                raise InvalidParams("arguments must be an object")
            missing = [k for k in tool["inputSchema"].get("required", []) if args.get(k) in (None, "")]
            if missing:
                return _text(f"missing argument(s): {', '.join(missing)}", error=True)
            fn = {"solve": self.solve, "check": self.check, "presets": self.presets}[name]
            try:
                return fn(args)
            except Exception as e:  # a tool that fails is a tool result with isError, as MCP expects
                return _text(f"{type(e).__name__}: {e}", error=True)
        raise MethodNotFound(method)

    def solve(self, a: dict) -> dict:
        d = Path(a["directory"]).expanduser()
        if not d.is_dir():
            return _text(f"{d} is not a directory", error=True)
        if (a.get("preset") or "openagent") not in PRESETS:
            return _text(f"unknown preset {a['preset']!r}: use one of {', '.join(PRESETS)}", error=True)
        strategy = with_overrides(PRESETS[a.get("preset") or "openagent"], rounds=a.get("rounds"))
        oc = replace(self.oc, model=a["model"]) if a.get("model") else self.oc
        self.log(f"solve in {d}: {a['task'][:80]}")
        out = solve(a["task"], d, a.get("check"), oc, strategy, a.get("protect") or [], log=self.log,
                    max_cost=a.get("max_cost"))
        result = _text(markdown(out, a["task"], a.get("check")), error=bool(out.error))
        result["structuredContent"] = out.to_dict() | {"attempts": len(out.attempts)}
        return result

    def check(self, a: dict) -> dict:
        d = Path(a["directory"]).expanduser()
        if not d.is_dir():
            return _text(f"{d} is not a directory", error=True)
        r = run_check(a["command"], d, a.get("timeout") or 600)
        head = f"{'passed' if r.passed else 'failed'} (exit {r.exit_code}) in {r.seconds:.1f}s"
        return _text(head if r.passed else head + "\n\n" + excerpt(r.output))

    def presets(self, _a: dict) -> dict:
        return _text("\n".join(f"- {n}: rounds={s.rounds}, attempts={s.attempts}, preamble={s.preamble}, "
                               f"reveal_check={s.reveal_check}" for n, s in PRESETS.items()))


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def serve(oc: OpenCode, stdin=None, stdout=None, log: Callable[[str], None] = lambda _: None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    server = Server(oc, log)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
