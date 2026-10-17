"""openagent: an agent on top of OpenCode that verifies and repairs its own work.

  openagent run "fix the failing test" --check "pytest -q"
  openagent bench examples/tasks --preset baseline --preset openagent --agentdiff
  openagent demo        # the whole workflow, offline: real OpenCode, scripted model, AgentDiff evals
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .agent import PRESETS, solve, with_overrides
from .bench import agentdiff_eval, run_bench, summarize, write_reports
from .opencode import OpenCode
from .tasks import discover
from .traces import to_trajectory, write_trajectory

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "tasks"
SYNTHETIC = ("OpenCode ran for real, with its own tools and sessions, but the model was a scripted "
             "playbook (openagent.sim), not an LLM. These numbers show the workflow, not how well any "
             "model or strategy does: the playbooks decide that.")


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
    g.add_argument("--attempts", type=int, help="parallel attempts; the passing one with fewest rounds wins")
    g.add_argument("--no-preamble", action="store_true", help="send the task without autonomy guidance")
    g.add_argument("--hide-check", action="store_true", help="don't tell the agent the check command")


def _make_oc(a, env=None) -> OpenCode:
    return OpenCode(binary=a.binary, model=a.model, agent=a.agent, auto=not a.no_auto,
                    extra_args=a.oc_arg, timeout=a.timeout, env=env)


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
    out = solve(task, a.dir, a.check, _make_oc(a), strategy, a.protect, a.check_timeout, log, a.max_cost)
    if out.error:
        log(out.error)
        return 2
    s = out.stats
    if s.last_text:
        print(s.last_text)
    ch = out.best.changes if out.best else {}
    touched = sum(len(v) for v in ch.values())
    log(f"{'PASSED' if out.passed else 'FAILED'} ({out.best.stop}) after {out.repairs} repair(s), "
        f"{touched} file(s) changed, {s.total_tokens:,} tokens, ${s.cost:.4f}, {out.seconds:.0f}s")
    if a.json:
        Path(a.json).write_text(json.dumps(out.to_dict(), indent=2))
    if a.trace:
        p = write_trajectory(to_trajectory(out, "task", task, a.check, 1, a.model), a.trace)
        log(f"wrote AgentDiff trace {p}")
    return 0 if out.passed else 1


def _bench(tasks, strategies, oc, a, synthetic=None) -> int:
    out = Path(a.out)
    log(f"{len(tasks)} task(s) x {len(strategies)} strategy(ies) x {a.repeat} repeat(s)")
    results = run_bench(tasks, strategies, oc, a.repeat, a.jobs, log, out / "traces", synthetic)
    ad = agentdiff_eval(out / "traces", out / "agentdiff", log=log) if a.agentdiff else None
    js, md = write_reports(results, out, {"model": oc.model, "tasks": len(tasks), "repeat": a.repeat,
                                          "strategies": [s.__dict__ for s in strategies], "synthetic": synthetic}, ad)
    print()
    for row in summarize(results):
        print(f"{row['strategy']:<12} {row['passed']}/{row['runs']} passed ({row['pass_rate']:.0%})  "
              f"avg ${row['avg_cost']:.4f}  avg {row['avg_seconds']:.0f}s")
    for r in ad or []:
        print(f"\nAgentDiff {r['pair']}:\n" + (r.get("error") or r["summary"]))
    log(f"wrote {md}, {js} and traces in {out / 'traces'}")
    return 0


def cmd_bench(a) -> int:
    tasks = discover(a.tasks)
    if a.only:
        tasks = [t for t in tasks if t.name in set(a.only)]
    if not tasks:
        log(f"no tasks found in {a.tasks}")
        return 2
    strategies = [_override(PRESETS[p], a) for p in (a.preset or ["baseline", "openagent"])]
    return _bench(tasks, strategies, _make_oc(a), a)


def cmd_demo(a) -> int:
    from .sim import ScriptedModel
    if shutil.which(a.binary) is None:
        log(f"`{a.binary}` not found. The demo runs the real OpenCode: npm i -g opencode-ai")
        return 2
    root = Path(a.tasks)
    if not root.is_dir():
        log(f"no tasks at {root}. The demo's tasks live in the repository: git clone "
            "https://github.com/sugeerth/Opencodeharness && cd Opencodeharness && pip install -e .")
        return 2
    tasks = [t for t in discover(root) if (root / t.name / "sim.json").is_file()]
    if a.only:
        tasks = [t for t in tasks if t.name in set(a.only)]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    strategies = [_override(PRESETS[p], a) for p in (a.preset or ["baseline", "guided", "openagent"])]
    with tempfile.TemporaryDirectory(prefix="openagent-sim-") as cfg, \
            ScriptedModel([root / t.name for t in tasks], Path(cfg)) as sim:
        log(f"scripted model serving at {sim.url}; OpenCode will use {sim.MODEL}")
        a.model = sim.MODEL
        return _bench(tasks, strategies, _make_oc(a, sim.env()), a, SYNTHETIC)


def cmd_presets(_a) -> int:
    for name, s in PRESETS.items():
        print(f"{name:<10} preamble={s.preamble} reveal_check={s.reveal_check} "
              f"rounds={s.rounds} attempts={s.attempts}")
    return 0


def _bench_args(p, default_out: str) -> None:
    p.add_argument("--preset", action="append", choices=PRESETS,
                   help="strategy to run (repeatable; default: baseline and openagent)")
    p.add_argument("--only", action="append", metavar="TASK", help="run only these tasks (repeatable)")
    p.add_argument("--repeat", type=int, default=1, help="runs per task and strategy")
    p.add_argument("-j", "--jobs", type=int, default=1, help="runs in parallel")
    p.add_argument("-o", "--out", default=default_out, help=f"output directory (default: {default_out})")
    p.add_argument("--agentdiff", action="store_true",
                   help="also evaluate the traces with AgentDiff (agentdiff runs + eval, each strategy vs baseline)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="openagent", description="An agent on top of OpenCode that verifies and repairs its own work.")
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
    r.add_argument("--max-cost", type=float, metavar="USD", help="stop once the runs have cost this much")
    r.add_argument("--json", metavar="FILE", help="write the full run record as JSON")
    r.add_argument("--trace", metavar="DIR", help="write the run as an AgentDiff trace into DIR")
    _opencode_args(r)
    _strategy_args(r)
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("bench", help="compare strategies on a task suite")
    b.add_argument("tasks", help="directory of tasks (see examples/tasks)")
    _bench_args(b, "results")
    _opencode_args(b)
    _strategy_args(b)
    b.set_defaults(fn=cmd_bench)

    d = sub.add_parser("demo", help="run the whole workflow offline: real OpenCode, a scripted model, AgentDiff evals")
    d.add_argument("--tasks", default=str(EXAMPLES), help="tasks with sim.json playbooks (default: the examples)")
    _bench_args(d, "demo-results")
    _opencode_args(d)
    _strategy_args(d)
    d.set_defaults(fn=cmd_demo)

    sub.add_parser("presets", help="list strategy presets").set_defaults(fn=cmd_presets)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
