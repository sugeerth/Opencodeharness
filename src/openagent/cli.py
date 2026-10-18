"""openagent: an agent on top of OpenCode that verifies and repairs its own work.

  openagent run "fix the failing test" --check "pytest -q"
  openagent bench examples/tasks --preset baseline --preset openagent --agentdiff
  openagent demo        # the whole workflow, offline: real OpenCode, scripted model, AgentDiff evals
  openagent evolve examples/tasks -m anthropic/claude-sonnet-5-5   # a harness that improves itself
  openagent ui          # all of it in a web front end
"""
import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import __version__
from .agent import PRESETS, Strategy, solve, with_overrides
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
    g.add_argument("--harness", metavar="FILE",
                   help="a harness file, e.g. the harness.json `openagent evolve` writes "
                        "(run: use it; bench/demo: add it to the strategies compared)")


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
    strategy = _override(Strategy.load(a.harness) if a.harness else PRESETS[a.preset], a)
    if not a.check:
        log("no --check given: OpenCode runs once with no verification or repair")
    out = solve(task, a.dir, a.check, _make_oc(a), strategy, a.protect, a.check_timeout, log, a.max_cost)
    if out.error:
        log(out.error)
        return 2
    s = out.stats
    if out.final_text:
        print(out.final_text)
    ch = out.applied_changes
    touched = sum(len(v) for v in ch.values())
    log(f"{'PASSED' if out.passed else 'FAILED'} ({out.best.stop}) after {out.repairs} repair(s), "
        f"{touched} file(s) changed, {s.total_tokens:,} tokens, ${s.cost:.4f}, {out.seconds:.0f}s")
    if a.json:
        Path(a.json).write_text(json.dumps(out.to_dict(), indent=2))
    if a.trace:
        p = write_trajectory(to_trajectory(out, "task", task, a.check, 1, a.model), a.trace)
        log(f"wrote AgentDiff trace {p}")
    return 0 if out.passed else 1


def _shard(text: str | None) -> tuple[int, int]:
    if not text:
        return 1, 1
    i, n = (int(x) for x in text.split("/"))
    if not 1 <= i <= n:
        raise SystemExit(f"--shard {text}: expected i/n with 1 <= i <= n")
    return i, n


def _bench(tasks, strategies, oc, a, synthetic=None) -> int:
    from .opencode import SharedServer
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    runs_log = out / "runs.jsonl"
    if runs_log.exists() and not getattr(a, "resume", False):
        runs_log.unlink()  # a fresh benchmark; --resume keeps finished runs
    shard = _shard(getattr(a, "shard", None))
    log(f"{len(tasks)} task(s) x {len(strategies)} strategy(ies) x {a.repeat} repeat(s)"
        + (f", shard {shard[0]}/{shard[1]}" if shard != (1, 1) else ""))
    server = getattr(a, "server", None)
    if server == "auto":
        with SharedServer(oc) as url:
            log(f"sharing one OpenCode server: {url}")
            results = run_bench(tasks, strategies, replace(oc, attach=url), a.repeat, a.jobs, log, out / "traces",
                                synthetic, shard, runs_log)
    else:
        results = run_bench(tasks, strategies, replace(oc, attach=server) if server else oc, a.repeat, a.jobs, log,
                            out / "traces", synthetic, shard, runs_log)
    return _report(results, out, {"model": oc.model, "tasks": len(tasks), "repeat": a.repeat,
                                  "strategies": [s.to_dict() for s in strategies], "synthetic": synthetic,
                                  "shard": f"{shard[0]}/{shard[1]}"}, a.agentdiff)


def _report(results, out: Path, meta: dict, agentdiff: bool) -> int:
    ad = agentdiff_eval(out / "traces", out / "agentdiff", log=log) if agentdiff else None
    js, md = write_reports(results, out, meta, ad)
    print()
    for row in summarize(results):
        print(f"{row['strategy']:<12} {row['passed']}/{row['runs']} passed ({row['pass_rate']:.0%})  "
              f"avg ${row['avg_cost']:.4f}  avg {row['avg_seconds']:.0f}s")
    for r in ad or []:
        print(f"\nAgentDiff {r['pair']}:\n" + (r.get("error") or r["summary"]))
    log(f"wrote {md}, {js} and traces in {out / 'traces'}")
    return 0


def cmd_merge(a) -> int:
    from .bench import merge_runs
    results = merge_runs(a.dirs, a.out)
    if not results:
        log("no runs found (each directory needs a runs.jsonl)")
        return 2
    metas = [json.loads((Path(d) / "results.json").read_text()).get("meta", {}) for d in a.dirs
             if (Path(d) / "results.json").is_file()]
    meta = {**(metas[0] if metas else {}), "merged_from": [str(d) for d in a.dirs], "shard": None}
    log(f"merged {len(results)} run(s) from {len(a.dirs)} director(ies)")
    return _report(results, Path(a.out), meta, a.agentdiff)


def cmd_bench(a) -> int:
    tasks = _tasks(a)
    if not tasks:
        log(f"no tasks found in {a.tasks}")
        return 2
    return _bench(tasks, _strategies(a, ["baseline", "openagent"]), _make_oc(a), a)


def _strategies(a, default: list[str]) -> list[Strategy]:
    strategies = [_override(PRESETS[p], a) for p in (a.preset or ([] if a.harness else default))]
    if a.harness:
        h = Strategy.load(a.harness)
        if not any(s.name == "baseline" for s in strategies):
            strategies.insert(0, PRESETS["baseline"])
        strategies.append(_override(h, a))
    return strategies


def _tasks(a) -> list:
    from .tasks import resolve
    tasks = discover(resolve(a.tasks, Path(a.out) / "tasks", log))
    if a.only:
        tasks = [t for t in tasks if t.name in set(a.only)]
    return tasks


def cmd_evolve(a) -> int:
    tasks = _tasks(a)
    if not tasks:
        log(f"no tasks found in {a.tasks}")
        return 2
    return _evolve(tasks, _make_oc(a), a)


def _evolve(tasks, oc, a, synthetic=None) -> int:
    from .evolve import evolve
    base = _override(Strategy.load(a.harness) if a.harness else PRESETS["openagent"], a)
    ledger = evolve(tasks, base, oc, a.out, a.generations, a.repeat, a.jobs, log, synthetic, a.agentdiff)
    print()
    for h in ledger["history"]:
        print(f"{h['version']:<4} {sum(h['rates'].values()):g}/{len(h['rates'])} tasks passing")
    for e in ledger["generations"]:
        print(f"gen {e['generation']}: {e['candidate']:<16} {e['verdict']:<9} {e['reason']}")
    print(f"stopped: {ledger.get('stopped', '')}")
    log(f"wrote {Path(a.out) / 'EVOLUTION.md'}, evolution.json and harness.json "
        f"(use it: openagent run --harness {Path(a.out) / 'harness.json'} ...)")
    return 0


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
    tasks = [t for t in _tasks(a) if (root / t.name / "sim.json").is_file()]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openagent-sim-") as cfg, \
            ScriptedModel([root / t.name for t in tasks], Path(cfg)) as sim:
        log(f"scripted model serving at {sim.url}; OpenCode will use {sim.MODEL}")
        a.model = sim.MODEL
        oc = _make_oc(a, sim.env())
        if a.evolve:
            a.generations = a.evolve
            return _evolve(tasks, oc, a, SYNTHETIC)
        return _bench(tasks, _strategies(a, ["baseline", "guided", "openagent"]), oc, a, SYNTHETIC)


def cmd_tasks_from_git(a) -> int:
    from .gittasks import build
    report = build(a.repo, a.out, a.test_cmd, a.max, a.look, a.reveal_tests, a.setup, a.timeout, a.branch, log)
    print(f"{len(report['taken'])} task(s) written to {a.out}; {len(report['skipped'])} commit(s) skipped "
          f"(reasons in {Path(a.out) / 'from-git.json'})")
    for t in report["taken"]:
        print(f"  {t['task']}")
    return 0 if report["taken"] else 1


def cmd_ui(a) -> int:
    from .ui.server import serve
    serve(a.port, a.home, not a.no_browser)
    return 0


def cmd_tasks_export(a) -> int:
    from .gittasks import export_manifest
    m = export_manifest(a.dirs, a.name)
    Path(a.out).write_text(json.dumps(m, indent=2) + "\n")
    print(f"{m['tasks']} task(s) from {len(m['repos'])} repositor(ies) written to {a.out}")
    return 0 if m["tasks"] else 1


def cmd_tasks_fetch(a) -> int:
    from .gittasks import fetch
    r = fetch(json.loads(Path(a.manifest).read_text()), a.out, not a.no_validate, a.timeout, a.only, log)
    print(f"{len(r['written'])} task(s) written to {a.out}; {len(r['failed'])} skipped (reasons in {Path(a.out) / 'fetch.json'})")
    return 0 if r["written"] else 1


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
    p.add_argument("--resume", action="store_true",
                   help="keep the runs already finished in OUT/runs.jsonl and run only the rest")
    p.add_argument("--shard", metavar="I/N", help="run only every N-th job starting at the I-th, to split a suite "
                                                 "across machines; combine the outputs with `openagent merge`")
    p.add_argument("--server", metavar="auto|URL",
                   help="reuse one OpenCode server for every run instead of a cold start each time: 'auto' starts "
                        "one for the benchmark, or give the URL of a running `opencode serve`")


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
    b.add_argument("tasks", help="directory of tasks (see examples/tasks), or a manifest such as benchmarks/realworld-v1.json")
    _bench_args(b, "results")
    _opencode_args(b)
    _strategy_args(b)
    b.set_defaults(fn=cmd_bench)

    d = sub.add_parser("demo", help="run the whole workflow offline: real OpenCode, a scripted model, AgentDiff evals")
    d.add_argument("--tasks", default=str(EXAMPLES), help="tasks with sim.json playbooks (default: the examples)")
    d.add_argument("--evolve", type=int, metavar="N", help="evolve the harness for N generations instead of benchmarking")
    _bench_args(d, "demo-results")
    _opencode_args(d)
    _strategy_args(d)
    d.set_defaults(fn=cmd_demo)

    e = sub.add_parser("evolve", help="improve the harness on your tasks, keeping a change only when the runs say it helped")
    e.add_argument("tasks", help="directory of tasks, or a manifest (.json)")
    e.add_argument("-g", "--generations", type=int, default=5, help="changes to try at most (default 5)")
    _bench_args(e, "evolution")
    _opencode_args(e)
    _strategy_args(e)
    e.set_defaults(fn=cmd_evolve)

    t = sub.add_parser("tasks", help="make benchmark tasks")
    tsub = t.add_subparsers(dest="tasks_cmd", required=True)
    fg = tsub.add_parser("from-git", help="tasks from a repository's history: the commit's tests grade the change")
    fg.add_argument("repo", help="path or URL of a git repository")
    fg.add_argument("-o", "--out", default="tasks", help="output directory (default: tasks)")
    fg.add_argument("--test-cmd", default="python3 -m pytest -q",
                    help="the project's test command; {tests} is replaced by the test files, else they are appended "
                         "(default: python3 -m pytest -q)")
    fg.add_argument("-n", "--max", type=int, default=10, help="tasks to make at most (default 10)")
    fg.add_argument("--look", type=int, default=200, help="commits to look at (default 200)")
    fg.add_argument("--branch", default="HEAD")
    fg.add_argument("--reveal-tests", action="store_true",
                    help="put the commit's tests in the workspace as the visible check, instead of holding them out")
    fg.add_argument("--setup", help="shell command to prepare a checkout, e.g. 'pip install -e .'")
    fg.add_argument("--timeout", type=int, default=600, help="seconds per test run while validating")
    fg.set_defaults(fn=cmd_tasks_from_git)
    ex = tsub.add_parser("export", help="write a manifest (a benchmark as a small file) from from-git task directories")
    ex.add_argument("dirs", nargs="+", help="directories written by `tasks from-git`")
    ex.add_argument("-o", "--out", default="manifest.json")
    ex.add_argument("--name", default="benchmark")
    ex.set_defaults(fn=cmd_tasks_export)
    fe = tsub.add_parser("fetch", help="rebuild a manifest's tasks from the repositories")
    fe.add_argument("manifest", help="manifest JSON, e.g. benchmarks/realworld-v1.json")
    fe.add_argument("-o", "--out", default="tasks", help="output directory (default: tasks)")
    fe.add_argument("--no-validate", action="store_true", help="skip re-running each commit's tests on parent and commit")
    fe.add_argument("--only", action="append", metavar="REPO", help="only repositories whose URL contains this")
    fe.add_argument("--timeout", type=int, default=600)
    fe.set_defaults(fn=cmd_tasks_fetch)

    m = sub.add_parser("merge", help="combine benchmark outputs (shards or resumed pieces) into one report")
    m.add_argument("dirs", nargs="+", help="benchmark output directories, each with a runs.jsonl")
    m.add_argument("-o", "--out", default="merged", help="output directory (default: merged)")
    m.add_argument("--agentdiff", action="store_true", help="also evaluate the merged traces with AgentDiff")
    m.set_defaults(fn=cmd_merge)

    u = sub.add_parser("ui", help="open the web front end: start runs, benchmarks, evolutions and the demo, and read the results")
    u.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765; 0 picks a free one)")
    u.add_argument("--home", help="where jobs are kept (default ~/.openagent/ui)")
    u.add_argument("--no-browser", action="store_true", help="don't open a browser")
    u.set_defaults(fn=cmd_ui)

    sub.add_parser("presets", help="list strategy presets").set_defaults(fn=cmd_presets)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
