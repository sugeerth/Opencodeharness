"""Benchmark strategies against a task suite and report how much each one improves on raw OpenCode."""
import json
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .agent import Strategy, solve
from .opencode import OpenCode
from .tasks import Task
from .verify import run_check


def run_one(task: Task, strategy: Strategy, oc: OpenCode, repeat: int = 0,
            log: Callable[[str], None] = lambda _: None) -> dict:
    """Run one task with one strategy in a fresh scratch workspace and return a result record."""
    if task.timeout:
        oc = replace(oc, timeout=task.timeout)
    tag = f"{task.name}/{strategy.name}" + (f"#{repeat + 1}" if repeat else "")
    with tempfile.TemporaryDirectory(prefix=f"openagent-{task.name}-") as tmp:
        ws = Path(tmp) / "ws"
        if task.files:
            shutil.copytree(task.files, ws, symlinks=True)
        else:
            ws.mkdir()
        if task.setup:
            s = run_check(task.setup, ws)
            if not s.passed:
                log(f"{tag}: setup failed")
                return {"task": task.name, "strategy": strategy.name, "repeat": repeat, "passed": False,
                        "error": "setup failed: " + s.output[-500:]}
        out = solve(task.prompt, ws, task.check, oc, strategy, task.protect,
                    log=lambda m: log(f"{tag}: {m}"))
    return {"task": task.name, "repeat": repeat, **out.to_dict()}


def run_bench(tasks: list[Task], strategies: list[Strategy], oc: OpenCode, repeat: int = 1, jobs: int = 1,
              log: Callable[[str], None] = lambda _: None) -> list[dict]:
    jobs_list = [(t, s, r) for r in range(repeat) for s in strategies for t in tasks]

    def go(job):
        t, s, r = job
        rec = run_one(t, s, oc, r, log)
        log(f"{'PASS' if rec['passed'] else 'FAIL'} {t.name}/{s.name}" + (f"#{r + 1}" if repeat > 1 else "")
            + (f"  ${rec['stats']['cost']:.4f} {rec['seconds']:.0f}s" if "stats" in rec else ""))
        return rec

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(go, jobs_list))


def summarize(results: list[dict]) -> list[dict]:
    rows = {}
    for r in results:
        rows.setdefault(r["strategy"], []).append(r)
    out = []
    for name, rs in rows.items():
        n = len(rs)
        stats = [r["stats"] for r in rs if "stats" in r]
        mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
        out.append({
            "strategy": name, "runs": n,
            "passed": sum(r["passed"] for r in rs),
            "pass_rate": sum(r["passed"] for r in rs) / n if n else 0.0,
            "first_try": sum(r["passed"] and r.get("repairs", 0) == 0 for r in rs),
            "avg_cost": mean([s["cost"] for s in stats]),
            "avg_tokens": mean([s["tokens"]["total"] for s in stats]),
            "avg_seconds": mean([r.get("seconds", 0) for r in rs]),
            "avg_repairs": mean([r.get("repairs", 0) for r in rs]),
            "total_cost": sum(s["cost"] for s in stats),
        })
    return out


def markdown_report(results: list[dict], meta: dict) -> str:
    summary = summarize(results)
    base = next((s for s in summary if s["strategy"] == "baseline"), None)
    lines = [f"# Open Agent benchmark", "",
             f"- Model: `{meta.get('model') or 'OpenCode default'}`",
             f"- Tasks: {meta.get('tasks')}, repeats: {meta.get('repeat', 1)}",
             f"- Date: {meta.get('date')}", "",
             "| Strategy | Pass rate | Passed | Δ vs baseline | First try | Avg repairs | Avg cost | Avg tokens | Avg time |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        delta = "—" if base is None or s is base else f"{(s['pass_rate'] - base['pass_rate']) * 100:+.0f} pts"
        lines.append(f"| {s['strategy']} | {s['pass_rate']:.0%} | {s['passed']}/{s['runs']} | {delta} | "
                     f"{s['first_try']} | {s['avg_repairs']:.2f} | ${s['avg_cost']:.4f} | "
                     f"{s['avg_tokens']:,.0f} | {s['avg_seconds']:.0f}s |")
    strategies = [s["strategy"] for s in summary]
    tasks = sorted({r["task"] for r in results})
    lines += ["", "## Per task", "", "| Task | " + " | ".join(strategies) + " |",
              "|---|" + "---|" * len(strategies)]
    for t in tasks:
        cells = []
        for s in strategies:
            rs = [r for r in results if r["task"] == t and r["strategy"] == s]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_reports(results: list[dict], out_dir: str | Path, meta: dict) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"date": time.strftime("%Y-%m-%d %H:%M"), **meta}
    js, md = out / "results.json", out / "report.md"
    js.write_text(json.dumps({"meta": meta, "summary": summarize(results), "results": results}, indent=2))
    md.write_text(markdown_report(results, meta))
    return js, md
