"""openagent: make OpenCode verify and repair its own work.

  openagent run "fix the failing test" --check "pytest -q"
  openagent bench examples/tasks --preset baseline --preset openagent
"""
import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .agent import PRESETS, solve, with_overrides
from .bench import run_bench, summarize, write_reports
from .opencode import OpenCode
from .tasks import discover


def log(msg: str) -> None:
    print(f"openagent: {msg}", file=sys.stderr, flush=True)


def _opencode_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("OpenCode")
    g.add_argument("-m", "--model", help="provider/model, e.g. anthropic/claude-sonnet-5-5")
    g.add_argument("--agent", help="OpenCode agent, e.g. build")
    g.add_argument("--binary", default="opencode", help="path to the opencode executable")
    g.add_argument("--timeout", type=int, default=900, help="seconds per OpenCode run (default 900)")
    g.add_argument("--no-auto", action="store_true", help="don't pass --auto (OpenCode may then block on permissions)")
    g.add_argument("--oc-arg", action="append", default=[], metavar="ARG",
                   help="extra argument for `opencode run` (repeatable), e.g. --oc-arg=--thinking")


def _strategy_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("strategy overrides")
    g.add_argument("--rounds", type=int, help="repair rounds after the first try")
    g.add_argument("--attempts", type=int, help="parallel attempts; the cheapest passing one wins")
    g.add_argument("--no-preamble", action="store_true", help="send the task without autonomy guidance")
    g.add_argument("--hide-check", action="store_true", help="don't tell the agent the check command")


def _make_oc(a) -> OpenCode:
    return OpenCode(binary=a.binary, model=a.model, agent=a.agent, auto=not a.no_auto,
                    extra_args=a.oc_arg, timeout=a.timeout)


def _override(strategy, a):
    return with_overrides(strategy, rounds=a.rounds, attempts=a.attempts,
                          preamble=False if a.no_preamble else None,
                          reveal_check=False if a.hide_check else None)


def cmd_run(a) -> int:
    task = " ".join(a.task) if a.task else sys.stdin.read().strip()
    if not task:
        log("no task given")
        return 2
    strategy = _override(PRESETS[a.preset], a)
    if not a.check:
        log("no --check given: OpenCode runs once with no verification or repair")
    out = solve(task, a.dir, a.check, _make_oc(a), strategy, a.protect, a.check_timeout, log)
    s = out.stats
    if s.last_text:
        print(s.last_text)
    log(f"{'PASSED' if out.passed else 'FAILED'} after {out.repairs} repair(s), "
        f"{s.total_tokens:,} tokens, ${s.cost:.4f}, {out.seconds:.0f}s")
    if a.json:
        Path(a.json).write_text(json.dumps(out.to_dict(), indent=2))
    return 0 if out.passed else 1


def cmd_bench(a) -> int:
    tasks = discover(a.tasks)
    if a.only:
        tasks = [t for t in tasks if t.name in set(a.only)]
    if not tasks:
        log(f"no tasks found in {a.tasks}")
        return 2
    strategies = [_override(PRESETS[p], a) for p in (a.preset or ["baseline", "openagent"])]
    log(f"{len(tasks)} task(s) x {len(strategies)} strategy(ies) x {a.repeat} repeat(s)")
    results = run_bench(tasks, strategies, _make_oc(a), a.repeat, a.jobs, log)
    js, md = write_reports(results, a.out, {"model": a.model, "tasks": len(tasks), "repeat": a.repeat,
                                            "strategies": [s.__dict__ for s in strategies]})
    for row in summarize(results):
        print(f"{row['strategy']:<12} {row['passed']}/{row['runs']} passed ({row['pass_rate']:.0%})  "
              f"avg ${row['avg_cost']:.4f}  avg {row['avg_seconds']:.0f}s")
    log(f"wrote {md} and {js}")
    return 0


def cmd_presets(_a) -> int:
    for name, s in PRESETS.items():
        print(f"{name:<10} preamble={s.preamble} reveal_check={s.reveal_check} "
              f"rounds={s.rounds} attempts={s.attempts}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="openagent", description="Make OpenCode verify and repair its own work.")
    p.add_argument("--version", action="version", version=f"openagent {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="solve a task in a directory, verifying and repairing until the check passes")
    r.add_argument("task", nargs="*", help="what to do (read from stdin if omitted)")
    r.add_argument("-C", "--dir", default=".", help="repository to work in (default: current directory)")
    r.add_argument("-c", "--check", help="shell command that must exit 0, e.g. 'pytest -q'")
    r.add_argument("--protect", action="append", default=[], metavar="GLOB",
                   help="files the agent may not change, e.g. 'tests/**' (restored before each check)")
    r.add_argument("--preset", choices=PRESETS, default="openagent")
    r.add_argument("--check-timeout", type=int, default=600)
    r.add_argument("--json", metavar="FILE", help="write the full run record as JSON")
    _opencode_args(r)
    _strategy_args(r)
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("bench", help="compare strategies on a task suite")
    b.add_argument("tasks", help="directory of tasks (see examples/tasks)")
    b.add_argument("--preset", action="append", choices=PRESETS,
                   help="strategy to run (repeatable; default: baseline and openagent)")
    b.add_argument("--only", action="append", metavar="TASK", help="run only these tasks (repeatable)")
    b.add_argument("--repeat", type=int, default=1, help="runs per task and strategy")
    b.add_argument("-j", "--jobs", type=int, default=1, help="runs in parallel")
    b.add_argument("-o", "--out", default="results", help="output directory (default: results)")
    _opencode_args(b)
    _strategy_args(b)
    b.set_defaults(fn=cmd_bench)

    sub.add_parser("presets", help="list strategy presets").set_defaults(fn=cmd_presets)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
