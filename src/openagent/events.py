"""Parse the JSON event stream that `opencode run --format json` prints, one event per line.

Each event looks like {"type", "timestamp", "sessionID", ...}. The types we use:
  text        part.text                       assistant text
  tool_use    part.tool, part.state.status    a finished tool call ("completed" or "error")
  step_finish part.cost, part.tokens          one model step, with usage
  error       error.message                   a run-level error
"""
import json
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RunStats:
    session_id: str | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tools: Counter = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    errors: list[str] = field(default_factory=list)
    last_text: str = ""

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.reasoning_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    def merge(self, other: "RunStats") -> "RunStats":
        """Add another run's usage into this one (in place) and return self."""
        self.session_id = other.session_id or self.session_id
        for name in ("steps", "tool_calls", "tool_errors", "input_tokens", "output_tokens",
                     "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "cost"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.tools.update(other.tools)
        self.errors += other.errors
        self.last_text = other.last_text or self.last_text
        return self

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "steps": self.steps, "tool_calls": self.tool_calls,
                "tool_errors": self.tool_errors, "tools": dict(self.tools),
                "tokens": {"input": self.input_tokens, "output": self.output_tokens,
                           "reasoning": self.reasoning_tokens, "cache_read": self.cache_read_tokens,
                           "cache_write": self.cache_write_tokens, "total": self.total_tokens},
                "cost": round(self.cost, 6), "errors": self.errors}


def _num(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def parse_events(stdout: str) -> list[dict]:
    """Return the JSON objects in `stdout`, skipping any line that isn't one."""
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def summarize(events: list[dict]) -> RunStats:
    s = RunStats()
    for ev in events:
        s.session_id = ev.get("sessionID") or s.session_id
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        kind = ev.get("type")
        if kind == "text" and part.get("text"):
            s.last_text = part["text"]
        elif kind == "tool_use":
            s.tool_calls += 1
            s.tools[part.get("tool", "?")] += 1
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if state.get("status") == "error":
                s.tool_errors += 1
        elif kind == "step_finish":
            s.steps += 1
            s.cost += _num(part.get("cost"))
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            s.input_tokens += int(_num(tok.get("input")))
            s.output_tokens += int(_num(tok.get("output")))
            s.reasoning_tokens += int(_num(tok.get("reasoning")))
            s.cache_read_tokens += int(_num(cache.get("read")))
            s.cache_write_tokens += int(_num(cache.get("write")))
        elif kind == "error":
            err = ev.get("error")
            msg = err.get("message") or (err.get("data") or {}).get("message") if isinstance(err, dict) else err
            s.errors.append(str(msg or err))
    return s
