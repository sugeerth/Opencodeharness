"""A scripted model for OpenCode: an OpenAI-compatible chat server on localhost that plays a
playbook instead of thinking. Real OpenCode runs against it, with real tools, real sessions and
the real event stream, so the whole Open Agent workflow can be exercised and demonstrated
offline, without an API key. Its results say nothing about how well a real model does: the
playbooks decide that. Everything produced with it is labelled synthetic.

A playbook is `sim.json` beside a task's prompt.txt:

  {"first":  [action, ...],   # the model's first turn on the task
   "verify": [action, ...],   # appended to the first turn when the prompt asks the model to run
                              #   the tests before finishing (Open Agent's guidance does)
   "repair": [[action, ...],  # the turn after Open Agent's 1st repair message, 2nd, ...
              [action, ...]],
   "when":   {"phrase": {...}}  # sections that replace the above when the prompt contains the phrase,
                                #   e.g. an instruction `openagent evolve` added
  }

An action is one of
  {"read": "path"}                              # read a file
  {"write": "path", "from": "sim/file"}         # write a file from the task dir (or "content": "...")
  {"edit": "path", "old": "...", "new": "..."}  # replace text in a file
  {"bash": "command"}                           # run a shell command
  {"say": "text"}                               # final text; ends the turn
Paths are relative to the workspace; the server makes them absolute from the system prompt's
working directory, as OpenCode's tools require.
"""
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GUIDANCE_MARK = "run the relevant tests or checks yourself"
REPAIR_MARK = "Verification failed (repair "


@dataclass
class Playbook:
    prompt: str
    first: list[dict]
    verify: list[dict] = field(default_factory=list)
    repair: list[list[dict]] = field(default_factory=list)
    root: Path | None = None
    when: dict = field(default_factory=dict)

    @classmethod
    def load(cls, task_dir: Path) -> "Playbook":
        d = json.loads((task_dir / "sim.json").read_text())
        return cls((task_dir / "prompt.txt").read_text().strip(), d.get("first", []), d.get("verify", []),
                   d.get("repair", []), task_dir, d.get("when", {}))

    def under(self, conversation: str) -> "Playbook":
        """The playbook as it plays when the prompt contains an instruction it reacts to: each phrase
        in "when" that appears in the conversation replaces the sections it names."""
        pb = self
        for phrase, override in self.when.items():
            if phrase in conversation:
                pb = Playbook(pb.prompt, override.get("first", pb.first), override.get("verify", pb.verify),
                              override.get("repair", pb.repair), pb.root, {})
        return pb


def _text(content) -> str:
    if isinstance(content, list):
        return "\n".join(_text(c.get("text", "")) for c in content if isinstance(c, dict))
    if not isinstance(content, str):
        return ""
    # OpenCode sends a user message as a JSON string literal ("\"...\""); decode it.
    if len(content) >= 2 and content[0] == content[-1] == '"':
        try:
            decoded = json.loads(content, strict=False)  # newlines arrive unescaped
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
    return content


class Policy:
    """Decides the next model message from the conversation so far. Stateless: everything it needs
    is in the messages OpenCode sends, which is what makes repeated and parallel runs safe."""

    def __init__(self, playbooks: list[Playbook]):
        self.playbooks = playbooks

    def find(self, users: list[str]) -> Playbook | None:
        # the longest matching prompt wins, so a task whose prompt contains another's is not confused with it
        hits = [pb for pb in self.playbooks if any(pb.prompt in u for u in users)]
        return max(hits, key=lambda pb: len(pb.prompt)) if hits else None

    def respond(self, body: dict) -> dict:
        """Returns {"text": str} or {"tool": name, "args": dict}."""
        msgs = body.get("messages", [])
        if not body.get("tools"):  # OpenCode's title request
            return {"text": "Scripted run"}
        system = "\n".join(_text(m.get("content")) for m in msgs if m.get("role") == "system")
        m = re.search(r"Working directory:\s*(.+)", system)
        cwd = m.group(1).strip() if m else "."
        user_idx = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
        users = [_text(msgs[i].get("content")) for i in user_idx]
        pb = self.find(users)
        if pb is None:
            return {"text": "I don't have a playbook for this task."}
        pb = pb.under("\n".join(users))
        last_user = users[-1]
        done = sum(1 for m in msgs[user_idx[-1] + 1:] if m.get("role") == "assistant")
        if REPAIR_MARK in last_user:
            n = int(re.search(r"repair (\d+) of", last_user).group(1))
            script = pb.repair[min(n, len(pb.repair)) - 1] if pb.repair else [{"say": "I could not fix it."}]
        elif GUIDANCE_MARK in last_user and pb.verify:
            # A text reply ends the turn, so the first turn's closing words give way to the verify steps.
            first = pb.first[:-1] if pb.first and "say" in pb.first[-1] else pb.first
            script = first + pb.verify
        else:
            script = pb.first
        if done >= len(script):
            return {"text": "Done."}
        return self.action(script[done], cwd, pb)

    def action(self, a: dict, cwd: str, pb: Playbook) -> dict:
        def path(p: str) -> str:
            return p if p.startswith("/") else f"{cwd.rstrip('/')}/{p}"
        if "say" in a:
            return {"text": a["say"]}
        if "read" in a:
            return {"tool": "read", "args": {"filePath": path(a["read"])}}
        if "write" in a:
            content = a.get("content")
            if content is None:
                content = (pb.root / a["from"]).read_text()
            return {"tool": "write", "args": {"filePath": path(a["write"]), "content": content}}
        if "edit" in a:
            return {"tool": "edit", "args": {"filePath": path(a["edit"]), "oldString": a["old"], "newString": a["new"]}}
        if "bash" in a:
            return {"tool": "bash", "args": {"command": a["bash"], "description": a.get("why", "Run a command")}}
        raise ValueError(f"unknown action {a}")


def _usage(body: dict, reply: dict) -> dict:
    """Rough token counts (4 chars a token) so the pipeline's accounting has something real to sum."""
    prompt = sum(len(_text(m.get("content"))) for m in body.get("messages", [])) // 4
    out = len(json.dumps(reply)) // 4
    return {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out}


class _Handler(BaseHTTPRequestHandler):
    policy: Policy = None  # set per server
    calls: list = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "coder", "object": "model"}]})

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if os.environ.get("OPENAGENT_SIM_LOG"):
            with open(os.environ["OPENAGENT_SIM_LOG"], "a") as f:
                f.write(json.dumps(body) + "\n")
        try:
            reply = self.policy.respond(body)
        except Exception as e:  # a broken playbook should read as a model error, not hang OpenCode
            reply = {"text": f"[scripted model error: {e}]"}
        self.calls.append(reply)
        usage = _usage(body, reply)
        created = int(time.time())
        if not body.get("stream"):
            msg = {"role": "assistant", "content": reply.get("text")}
            if "tool" in reply:
                msg["tool_calls"] = [{"id": f"call_{len(self.calls)}", "type": "function",
                                      "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])}}]
            self._json({"id": "sim", "object": "chat.completion", "created": created, "model": "coder",
                        "choices": [{"index": 0, "message": msg,
                                     "finish_reason": "tool_calls" if "tool" in reply else "stop"}],
                        "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def send(delta, finish=None, with_usage=False):
            chunk = {"id": "sim", "object": "chat.completion.chunk", "created": created, "model": "coder",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if with_usage:
                chunk["usage"] = usage
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        send({"role": "assistant", "content": ""})
        if "tool" in reply:
            send({"tool_calls": [{"index": 0, "id": f"call_{len(self.calls)}", "type": "function",
                                  "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])}}]})
            send({}, "tool_calls", True)
        else:
            send({"content": reply["text"]})
            send({}, "stop", True)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class ScriptedModel:
    """`with ScriptedModel(task_dirs) as sim:` serves the playbooks on localhost; `sim.env()` is the
    environment that points OpenCode at it as the model `scripted/coder`."""

    MODEL = "scripted/coder"

    def __init__(self, task_dirs: list[Path], config_dir: Path, port: int = 0):
        playbooks = [Playbook.load(Path(d)) for d in task_dirs if (Path(d) / "sim.json").is_file()]
        handler = type("Handler", (_Handler,), {"policy": Policy(playbooks), "calls": []})
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.handler = handler
        # Absolute: OpenCode runs in each workspace and would resolve a relative path from there.
        self.config = Path(config_dir).resolve() / "opencode-scripted.json"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def env(self) -> dict[str, str]:
        return {"OPENCODE_CONFIG": str(self.config), "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

    def __enter__(self) -> "ScriptedModel":
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {"scripted": {"npm": "@ai-sdk/openai-compatible", "name": "Scripted (Open Agent demo)",
                                      "options": {"baseURL": self.url, "apiKey": "not-needed"},
                                      "models": {"coder": {"name": "Scripted coder"}}}}}))
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
