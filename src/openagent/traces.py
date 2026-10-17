"""Write Open Agent runs as AgentDiff traces (SCHEMA v1), so AgentDiff can evaluate them:
`agentdiff runs` for paired inference and pass^k between two strategies, `agentdiff compare`
for the step where two runs parted, `agentdiff eval` for the scorecard.

Each run becomes one trajectory. Its steps are, in order, for every round: the repair prompt
Open Agent sent (a `plan` step, from the second round on), what OpenCode did (its text,
reasoning and tool calls, with tokens and timing from the event stream), any protected files
the guard restored, and the check. The last step is the `answer`.

See https://github.com/sugeerth/Agenttracing/blob/main/SCHEMA.md
"""
import json
import re
from pathlib import Path

from . import __version__
from .agent import PREAMBLE, Outcome
from .verify import excerpt

MAX_FIELD = 4000

# OpenCode tool name -> AgentDiff step type. Searches are "search"; every other tool, file reads
# included, is a "tool_call" with its effect declared, because AgentDiff's read/write ledger only
# looks at tool calls and searches: a file read typed "read" would be invisible to it, and every
# write after it would look like a write made without looking.
TOOL_TYPES = {"list": "search", "glob": "search", "grep": "search", "codesearch": "search", "websearch": "search"}

# What each OpenCode tool does to the outside world, declared so AgentDiff doesn't have to guess
# from names. bash can do either, so it stays undeclared.
TOOL_EFFECTS = {"read": "read", "list": "read", "glob": "read", "grep": "read", "codesearch": "read",
                "websearch": "read", "webfetch": "read", "todoread": "read", "skill": "read",
                "write": "write", "edit": "write", "multiedit": "write", "patch": "write", "apply_patch": "write",
                "todowrite": "write", "openagent.check": "read", "openagent.guard": "write"}

# The tools OpenCode's build agent offers (as of 1.19), plus Open Agent's own steps.
OFFERED = set(TOOL_EFFECTS) | {"bash", "task", "question"}


# Open Agent's stop reason -> AgentDiff's closed `termination` vocabulary. The exact reason is
# kept in harness.stop. infrastructure_error is excluded from AgentDiff's reliability statistics,
# which is right: OpenCode failing to reach the model says nothing about the agent.
TERMINATION = {"passed": "agent_stop", "done": "agent_stop", "failed": "agent_stop", "stalled": "agent_stop",
               "rounds_exhausted": "max_steps", "budget": "max_steps", "agent_error": "agent_error",
               "infra_error": "infrastructure_error", "error": "unexpected_error"}


def _clip(s, n: int = MAX_FIELD) -> str:
    s = s if isinstance(s, str) else json.dumps(s, sort_keys=True, default=str)
    return s if len(s) <= n else s[:n] + f"... [{len(s) - n} chars clipped]"


def _num(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def safe_name(s: str) -> str:
    """AgentDiff's runs layout splits file names on '__', so keep names free of it."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]+", "-", s)).strip("-") or "x"


def steps_from_events(events: list[dict], t0_ms: float | None = None) -> tuple[list[dict], float | None]:
    """OpenCode's JSON events -> AgentDiff steps (without the final answer).

    Usage arrives on `step_finish` once per model step; it is put on the last step that model step
    produced. Latency is the time since the previous event."""
    steps: list[dict] = []
    block_start = 0
    prev_ms = t0_ms
    for ev in events:
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        ts = _num(ev.get("timestamp")) or None
        if t0_ms is None and ts:
            t0_ms = prev_ms = ts
        latency = max(0.0, (ts - prev_ms) / 1000) if ts and prev_ms else 0.0
        started = max(0.0, (prev_ms - t0_ms) / 1000) if prev_ms and t0_ms else None
        kind = ev.get("type")
        step = None
        if kind == "text" and str(part.get("text", "")).strip():
            step = {"type": "reason", "name": "text", "input": "", "output": _clip(part["text"])}
        elif kind == "reasoning" and str(part.get("text", "")).strip():
            step = {"type": "reason", "name": "thinking", "input": "", "output": _clip(part["text"])}
        elif kind == "tool_use":
            tool = str(part.get("tool") or "tool")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            failed = state.get("status") == "error"
            out = state.get("error") if failed else state.get("output")
            step = {"type": TOOL_TYPES.get(tool, "tool_call"), "name": tool, "input": _clip(state.get("input", "")),
                    "output": _clip(("ERROR: " if failed else "") + str(out if out is not None else "")),
                    "error": failed}
            if tool in TOOL_EFFECTS:
                step["effect"] = TOOL_EFFECTS[tool]
            if failed:
                step["note"] = "the tool call failed"
        elif kind == "step_finish":
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            inp, out = int(_num(tok.get("input"))), int(_num(tok.get("output")) + _num(tok.get("reasoning")))
            cached, written = int(_num(cache.get("read"))), int(_num(cache.get("write")))
            if len(steps) > block_start:
                last = steps[-1]
                last["tokens"] = last.get("tokens", 0) + inp + out + cached + written
                last["input_tokens"] = last.get("input_tokens", 0) + inp + cached + written
                last["output_tokens"] = last.get("output_tokens", 0) + out
                last["cached_tokens"] = last.get("cached_tokens", 0) + cached
            block_start = len(steps)
        elif kind == "error":
            err = ev.get("error")
            msg = (err.get("message") or (err.get("data") or {}).get("message")) if isinstance(err, dict) else err
            step = {"type": "reason", "name": "error", "input": "", "output": _clip(str(msg or err)),
                    "note": "OpenCode reported an error"}
        if ts:
            prev_ms = ts
        if step is not None:
            step.setdefault("tokens", 0)
            step["latency_s"] = round(latency, 3)
            if started is not None:
                step["started_s"] = round(started, 3)
            steps.append(step)
    return steps, t0_ms


def to_trajectory(outcome: Outcome, task_id: str, prompt: str, check: str | None, run: int = 1,
                  model: str | None = None, graded: bool | None = None, grade_output: str = "",
                  synthetic: str | None = None) -> dict:
    """One Open Agent run as an AgentDiff trajectory dict. `graded` (the held-out grader's verdict),
    when given, is the outcome; otherwise the check's verdict is."""
    s = outcome.strategy
    a = outcome.best
    steps: list[dict] = []
    t0 = None
    for i, r in enumerate(a.rounds if a else []):
        if i > 0:
            steps.append({"type": "plan", "name": "openagent.repair", "input": _clip(r.prompt), "output": "",
                          "tokens": 0, "latency_s": 0.0,
                          "note": "Open Agent sent the check's failure back to the same session"})
        evsteps, t0 = steps_from_events(r.events, t0)
        steps += evsteps
        if r.tampered:
            steps.append({"type": "tool_call", "name": "openagent.guard", "input": ", ".join(r.tampered),
                          "output": "restored protected files the agent changed", "tokens": 0, "latency_s": 0.0,
                          "error": False, "effect": "write", "quality": "bad",
                          "note": "the agent edited protected files"})
        if r.check is not None:
            step = {"type": "tool_call", "name": "openagent.check", "input": check or "",
                    "output": _clip(f"exit {r.check.exit_code}\n" + excerpt(r.check.output, 60, 20)),
                    "tokens": 0, "latency_s": round(r.check.seconds, 3),
                    # The check ran fine; what it found is the agent's result, not a tool error.
                    # Marking it an error makes AgentDiff blame the environment for the agent's bug.
                    "error": False, "effect": "read",
                    "note": "check passed" if r.check.passed else "check failed"}
            if i > 0:
                # The harness re-ran the check itself; in AgentDiff's schema that is an attempt, not
                # the agent asking for the same thing again.
                step["attempt"] = i + 1
            steps.append(step)
    stats = outcome.stats
    success = outcome.passed if graded is None else bool(graded)
    last_text = outcome.final_text.strip()
    verdict = ("passed" if outcome.passed else "failed") + (f" after {outcome.repairs} repair(s)" if a else "")
    answer = (f"check {verdict}" if check else ("finished" if outcome.passed else "failed"))
    if graded is not None:
        answer += f"; held-out grade {'passed' if graded else 'failed'}"
    if outcome.error:
        answer = outcome.error
    steps.append({"type": "answer", "name": "answer", "input": "", "output": _clip(last_text or answer),
                  "tokens": 0, "latency_s": 0.0})
    for i, st in enumerate(steps):
        st["index"] = i
    harness = {"adapter": f"openagent {__version__}", "graded_by": "grade" if graded is not None else "check",
               "stop": a.stop if a else "error", "repairs": outcome.repairs,
               "opencode_retries": sum(r.retried for r in a.rounds) if a else 0,
               "changes": outcome.applied_changes}
    if grade_output:
        harness["grade_output"] = _clip(grade_output, 2000)
    if synthetic:
        harness["note"] = "SYNTHETIC: " + synthetic
    return {
        "schema_version": 1,
        "trace_id": f"{safe_name(task_id)}__{safe_name(s.name)}__r{run}",
        "agent": {"name": s.name, "model": model or "opencode-default", "version": f"openagent-{__version__}",
                  "system_prompt": (PREAMBLE if s.preamble else "") + "".join(f"\n- {i}" for i in s.instructions),
                  "config": {**s.to_dict(), "check": check}},
        "task": {"id": task_id, "prompt": prompt, "expected": None},
        "outcome": {"success": success, "answer": answer, "score": 1.0 if success else 0.0,
                    # a timeout only ends the run if it was the last round
                    "termination": "timeout" if a and a.rounds and a.stop != "passed"
                                   and any("timed out" in e for e in a.rounds[-1].stats.errors)
                    else TERMINATION.get(a.stop if a else "error", "agent_stop")},
        "totals": {"input_tokens": stats.input_tokens + stats.cache_read_tokens + stats.cache_write_tokens,
                   "output_tokens": stats.output_tokens + stats.reasoning_tokens,
                   "cost_usd": round(stats.cost, 6), "latency_s": round(outcome.seconds, 3)},
        "harness": harness,
        "tools": [{"name": name, "effect": TOOL_EFFECTS.get(name)} for name in sorted(OFFERED)],
        "steps": steps,
    }


def write_trajectory(traj: dict, out_dir: str | Path) -> Path:
    """Write to <out_dir>/<task>__<agent>__<run>.json, the layout `agentdiff runs` reads."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{traj['trace_id']}.json"
    path.write_text(json.dumps(traj, indent=1))
    return path
