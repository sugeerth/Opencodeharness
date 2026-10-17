import argparse
import json
from pathlib import Path

from .opencode import Task, run_task


def main(argv=None):
    p = argparse.ArgumentParser(prog="ocharness", description="Run tasks through the OpenCode CLI and check results")
    p.add_argument("tasks", help="directory containing one sub-directory per task")
    p.add_argument("--model", help="provider/model, e.g. anthropic/claude-sonnet-5-5")
    p.add_argument("--agent")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--binary", default="opencode")
    p.add_argument("--json", metavar="FILE", help="write results as JSON")
    a = p.parse_args(argv)

    dirs = sorted(d for d in Path(a.tasks).iterdir() if (d / "prompt.txt").is_file())
    results = []
    for d in dirs:
        r = run_task(Task.load(d), a.model, a.agent, a.timeout, a.binary)
        results.append(r)
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.task}  ({r.seconds:.1f}s)" + ("" if r.passed else f"  {r.detail.strip()[:120]}"))
    print(f"{sum(r.passed for r in results)}/{len(results)} passed")
    if a.json:
        Path(a.json).write_text(json.dumps([{**r.__dict__, "events": len(r.events)} for r in results], indent=2))
    return 0 if all(r.passed for r in results) else 1
