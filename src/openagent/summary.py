"""A run as Markdown: what `openagent run --summary` writes, for a pull request body or a CI job summary.

It says what was asked, whether the check passed, how each round went (what the agent changed, what
the check said), what it cost, and the agent's last message: what a reviewer needs to decide whether
to look closer.
"""
from .agent import Outcome
from .verify import excerpt

STOP = {"passed": "the check passed", "done": "the agent finished (no check)", "agent_error": "OpenCode failed",
        "infra_error": "OpenCode could not reach its model", "stalled": "two rounds changed nothing",
        "budget": "the cost budget ran out", "rounds_exhausted": "the repair rounds ran out",
        "failed": "the check failed", "setup_failed": "the setup command failed"}


def _files(changes: dict) -> list[str]:
    marks = {"added": "+", "modified": "~", "deleted": "-"}
    return [f"`{marks[k]} {p}`" for k in ("added", "modified", "deleted") for p in changes.get(k, [])]


def markdown(out: Outcome, task: str, check: str | None, title: str = "Open Agent") -> str:
    if out.error:
        return f"## {title}: error\n\n{out.error}\n"
    b = out.best
    s = out.stats
    verdict = "passed" if out.passed else "did not pass"
    head = (f"## {title}: {'✅' if out.passed else '❌'} the check {verdict}" if check
            else f"## {title}: {'✅' if out.passed else '❌'} {STOP.get(b.stop, b.stop) if b else 'no run'}")
    lines = [head, ""]
    lines += ["> " + ln for ln in task.strip().splitlines()[:12]] + [""]
    if out.preflight and out.preflight.passed:
        lines += ["The check already passed before the agent started.", ""]
    facts = [("Strategy", f"`{out.strategy.name}`"),
             ("Check", f"`{check}`" if check else "none"),
             ("Ended", STOP.get(b.stop, b.stop) if b else "-"),
             ("Repairs", str(out.repairs)),
             ("Attempts", f"{len(out.attempts)}" + (f" (winner: #{out.winner + 1})"
                                                   if len(out.attempts) > 1 and out.winner is not None else "")),
             ("Tokens", f"{s.total_tokens:,}"), ("Cost", f"${s.cost:.4f}"), ("Time", f"{out.seconds:.0f}s")]
    lines += ["| | |", "|---|---|"] + [f"| {k} | {v} |" for k, v in facts] + [""]
    files = _files(out.applied_changes)
    lines += [f"**Changed** ({len(files)}): " + (" ".join(files) if files else "nothing"), ""]
    if b and b.rounds:
        lines += ["### Rounds", "", "| # | Tool calls | Files changed | Check |", "|---|---|---|---|"]
        for i, r in enumerate(b.rounds, 1):
            chk = "-" if r.check is None else ("pass" if r.check.passed else f"fail (exit {r.check.exit_code})")
            if r.tampered:
                chk += f", restored {len(r.tampered)} protected file(s)"
            lines.append(f"| {i} | {r.stats.tool_calls} | {len(r.changed)} | {chk} |")
        lines.append("")
        last = b.rounds[-1].check
        if last is not None and not last.passed:
            lines += ["<details><summary>Last check output</summary>", "", "```",
                      excerpt(last.output, max_lines=60, tail=20), "```", "", "</details>", ""]
    if out.final_text.strip():
        lines += ["<details><summary>The agent's last message</summary>", "", out.final_text.strip()[:4000], "",
                  "</details>", ""]
    lines += ["<sub>[Open Agent](https://github.com/sugeerth/opencodeharness) on "
              "[OpenCode](https://github.com/sst/opencode)</sub>", ""]
    return "\n".join(lines)
