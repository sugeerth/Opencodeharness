"""Benchmark strategies against a task suite and report how much each one improves on raw OpenCode.

Every run is graded twice when the task has a held-out grader: by the check the agent could see,
and by the grader it never saw. The held-out verdict is the one that counts. Every run is also
written as an AgentDiff trace, and each strategy is compared with `baseline` the way AgentDiff
decides whether to keep a harness change: per task, and kept only if it wins more tasks than it
loses and loses none.
"""
import json
import math
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .agent import Strategy, solve
from .opencode import OpenCode
from .tasks import Task
from .traces import safe_name, to_trajectory, write_trajectory
from .verify import excerpt, run_check

Log = Callable[[str], None]


def run_one(task: Task, strategy: Strategy, oc: OpenCode, repeat: int = 0, log: Log = lambda _: None,
            traces_dir: Path | None = None, synthetic: str | None = None) -> dict:
    """Run one task with one strategy in a fresh scratch workspace and return a result record."""
    if task.timeout:
        oc = replace(oc, timeout=task.timeout)
    tag = f"{task.name}/{strategy.name}" + (f"#{repeat + 1}" if repeat else "")
    base = {"task": task.name, "strategy": strategy.name, "repeat": repeat}
    with tempfile.TemporaryDirectory(prefix=f"openagent-{task.name}-") as tmp:
        ws = Path(tmp) / "ws"
        if task.files:
            shutil.copytree(task.files, ws, symlinks=True)
        else:
            ws.mkdir()
        if task.setup:
            s = run_check(task.setup, ws, sandbox=oc.sandbox)
            if not s.passed:
                log(f"{tag}: setup failed")
                return {**base, "passed": False, "check_passed": False, "stop": "setup_failed",
                        "error": "setup failed: " + s.output[-500:]}
        out = solve(task.prompt, ws, task.check, oc, strategy, task.protect, log=lambda m: log(f"{tag}: {m}"))
        graded, grade_output = None, ""
        if task.grade and not out.error:
            g = run_check(task.grade, ws, sandbox=oc.sandbox)
            graded, grade_output = g.passed, excerpt(g.output, 40, 15)
            log(f"{tag}: held-out grade {'passed' if g.passed else 'failed'}")
    rec = {**base, **out.to_dict(), "check_passed": out.passed,
           "passed": out.passed if graded is None else graded, "graded": graded}
    if traces_dir is not None:
        traj = to_trajectory(out, task.name, task.prompt, task.check, repeat + 1, oc.model, graded,
                             grade_output, synthetic)
        path = write_trajectory(traj, traces_dir / safe_name(strategy.name))
        rec["trace"] = path.relative_to(traces_dir.parent).as_posix()
    return rec


def run_key(task: str, strategy: str, repeat: int) -> str:
    return f"{task}\t{strategy}\t{repeat}"


def load_runs(path: Path) -> dict[str, dict]:
    """Finished runs from a runs.jsonl log, keyed by task/strategy/repeat. A torn last line (the process
    was killed mid-write) is ignored."""
    done: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            done[run_key(r["task"], r["strategy"], r.get("repeat", 0))] = r
    return done


def run_bench(tasks: list[Task], strategies: list[Strategy], oc: OpenCode, repeat: int = 1, jobs: int = 1,
              log: Log = lambda _: None, traces_dir: str | Path | None = None,
              synthetic: str | None = None, shard: tuple[int, int] = (1, 1),
              runs_log: str | Path | None = None) -> list[dict]:
    """Run every task x strategy x repeat. With `runs_log`, each finished run is appended to that JSONL
    file and runs already in it are not run again, so an interrupted benchmark resumes where it
    stopped. `shard=(i, n)` runs only every n-th job starting at the i-th, so n machines can split a
    suite and `merge_runs` can put the pieces together."""
    jobs_list = [(t, s, r) for r in range(repeat) for s in strategies for t in tasks]
    i, n = shard
    jobs_list = [job for k, job in enumerate(jobs_list) if k % n == i - 1]
    tdir = Path(traces_dir) if traces_dir else None
    log_path = Path(runs_log) if runs_log else None
    done = load_runs(log_path) if log_path else {}
    lock = threading.Lock()
    if done:
        log(f"resuming: {sum(run_key(t.name, s.name, r) in done for t, s, r in jobs_list)} of {len(jobs_list)} run(s) already done")

    def go(job):
        t, s, r = job
        prev = done.get(run_key(t.name, s.name, r))
        if prev is not None:
            return prev
        rec = run_one(t, s, oc, r, log, tdir, synthetic)
        if log_path:
            with lock, open(log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        extra = ""
        if "stats" in rec:
            extra = f"  ${rec['stats']['cost']:.4f} {rec['seconds']:.0f}s {rec.get('stop', '')}"
            if rec.get("graded") is not None and rec["graded"] != rec["check_passed"]:
                extra += f" (check {'passed' if rec['check_passed'] else 'failed'})"
        log(f"{'PASS' if rec['passed'] else 'FAIL'} {t.name}/{s.name}" + (f"#{r + 1}" if repeat > 1 else "") + extra)
        return rec

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(go, jobs_list))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def summarize(results: list[dict]) -> list[dict]:
    rows: dict[str, list[dict]] = {}
    for r in results:
        rows.setdefault(r["strategy"], []).append(r)
    out = []
    for name, rs in rows.items():
        n = len(rs)
        stats = [r["stats"] for r in rs if "stats" in r]
        stops: dict[str, int] = {}
        for r in rs:
            stops[r.get("stop", "error")] = stops.get(r.get("stop", "error"), 0) + 1
        out.append({
            "strategy": name, "runs": n,
            "passed": sum(r["passed"] for r in rs),
            "pass_rate": sum(r["passed"] for r in rs) / n if n else 0.0,
            "check_passed": sum(r.get("check_passed", r["passed"]) for r in rs),
            "graded_runs": sum(r.get("graded") is not None for r in rs),
            "first_try": sum(r["passed"] and r.get("repairs", 0) == 0 for r in rs),
            "avg_cost": _mean([s["cost"] for s in stats]),
            "avg_tokens": _mean([s["tokens"]["total"] for s in stats]),
            "avg_seconds": _mean([r.get("seconds", 0) for r in rs]),
            "avg_repairs": _mean([r.get("repairs", 0) for r in rs]),
            "total_cost": sum(s["cost"] for s in stats),
            "stops": stops,
        })
    return out


def sign_test(wins: int, losses: int) -> float | None:
    """Two-sided exact sign test p-value for wins vs losses (ties dropped)."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def paired(results: list[dict], baseline: str = "baseline") -> list[dict]:
    """Each strategy against `baseline`, task by task (pass rate over repeats). The verdict uses
    AgentDiff's rule for a harness change: keep it only if it wins more tasks than it loses and
    loses none. Below ten tasks the sign test cannot reach significance, and the row says so."""
    rate: dict[tuple[str, str], list[bool]] = {}
    for r in results:
        rate.setdefault((r["strategy"], r["task"]), []).append(bool(r["passed"]))
    tasks = sorted({r["task"] for r in results})
    strategies = list(dict.fromkeys(r["strategy"] for r in results))
    if baseline not in strategies:
        return []
    rows = []
    for s in strategies:
        if s == baseline:
            continue
        won, lost, tied = [], [], []
        for t in tasks:
            a, b = rate.get((s, t)), rate.get((baseline, t))
            if not a or not b:
                continue
            d = _mean(a) - _mean(b)
            (won if d > 0 else lost if d < 0 else tied).append(t)
        if won and not lost:
            verdict = "keep"
        elif len(won) > len(lost):
            verdict = "mixed: wins more than it loses but breaks a task"
        elif not won and not lost:
            verdict = "no difference"
        else:
            verdict = "revert"
        p = sign_test(len(won), len(lost))
        rows.append({"strategy": s, "vs": baseline, "wins": won, "losses": lost, "ties": tied,
                     "verdict": verdict, "sign_test_p": p,
                     "note": "fewer than 10 tasks: no sign test can be significant here" if len(won) + len(lost) + len(tied) < 10 else ""})
    return rows


def markdown_report(results: list[dict], meta: dict, agentdiff: list[dict] | None = None) -> str:
    summary = summarize(results)
    base = next((s for s in summary if s["strategy"] == "baseline"), None)
    lines = ["# Open Agent benchmark", ""]
    if meta.get("synthetic"):
        lines += [f"> **Synthetic run.** {meta['synthetic']}", ""]
    lines += [f"- Model: `{meta.get('model') or 'OpenCode default'}`",
              f"- Tasks: {meta.get('tasks')}, repeats: {meta.get('repeat', 1)}",
              f"- Date: {meta.get('date')}", ""]
    graded = any(s["graded_runs"] for s in summary)
    lines += ["| Strategy | Pass rate | Passed | Δ vs baseline |" + (" Visible check |" if graded else "")
              + " First try | Avg repairs | Avg cost | Avg tokens | Avg time | How runs ended |",
              "|---|---|---|---|" + ("---|" if graded else "") + "---|---|---|---|---|---|"]
    for s in summary:
        delta = "—" if base is None or s is base else f"{(s['pass_rate'] - base['pass_rate']) * 100:+.0f} pts"
        stops = ", ".join(f"{k} {v}" for k, v in sorted(s["stops"].items()))
        lines.append(f"| {s['strategy']} | {s['pass_rate']:.0%} | {s['passed']}/{s['runs']} | {delta} | "
                     + (f"{s['check_passed']}/{s['runs']} | " if graded else "")
                     + f"{s['first_try']} | {s['avg_repairs']:.2f} | ${s['avg_cost']:.4f} | "
                     f"{s['avg_tokens']:,.0f} | {s['avg_seconds']:.0f}s | {stops} |")
    if graded:
        lines += ["", "Pass rate is graded by held-out tests the agent never saw where a task has them; "
                      "*Visible check* is the check the agent could see and was repaired against."]
    pr = paired(results)
    if pr:
        lines += ["", "## Against baseline, task by task", "",
                  "AgentDiff's rule for a harness change: keep it only if it wins more tasks than it loses and loses none.", "",
                  "| Strategy | Wins | Losses | Ties | Sign test p | Verdict |", "|---|---|---|---|---|---|"]
        for r in pr:
            p = "—" if r["sign_test_p"] is None else f"{r['sign_test_p']:.3f}"
            lines.append(f"| {r['strategy']} | {', '.join(r['wins']) or '—'} | {', '.join(r['losses']) or '—'} | "
                         f"{len(r['ties'])} | {p} | {r['verdict']} |")
        notes = {r["note"] for r in pr if r["note"]}
        lines += [f"\n_{n}._" for n in notes]
    strategies = [s["strategy"] for s in summary]
    tasks = sorted({r["task"] for r in results})
    lines += ["", "## Per task", "", "| Task | " + " | ".join(strategies) + " |", "|---|" + "---|" * len(strategies)]
    for t in tasks:
        cells = []
        for s in strategies:
            rs = [r for r in results if r["task"] == t and r["strategy"] == s]
            cell = f"{sum(r['passed'] for r in rs)}/{len(rs)}"
            if any(r.get("graded") is not None and r["graded"] != r["check_passed"] for r in rs):
                cell += f" (check {sum(r['check_passed'] for r in rs)}/{len(rs)})"
            cells.append(cell)
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    if agentdiff:
        lines += ["", "## AgentDiff evaluation", ""]
        for a in agentdiff:
            lines += [f"### {a['pair']}", ""]
            if a.get("error"):
                lines += [a["error"], ""]
                continue
            lines += ["```", a["summary"].strip(), "```", "",
                      f"Full outputs: `{a['out']}/report.html` and the scorecard in `{a['eval']}`", ""]
    return "\n".join(lines) + "\n"


def agentdiff_eval(traces_dir: str | Path, out_dir: str | Path, baseline: str = "baseline",
                   binary: str = "agentdiff", log: Log = lambda _: None) -> list[dict]:
    """Run AgentDiff over the traces: for each strategy, `agentdiff runs` against baseline (paired
    inference, pass^k, the consolidated diagnosis) and `agentdiff eval` (the scorecard)."""
    traces_dir, out_dir = Path(traces_dir), Path(out_dir)
    if shutil.which(binary) is None:
        return [{"pair": "AgentDiff", "error": "AgentDiff is not installed: `pip install git+https://github.com/"
                 "sugeerth/Agenttracing`, then rerun with --agentdiff, or run `agentdiff runs` on each pair "
                 "directory yourself."}]
    names = sorted(p.name for p in traces_dir.iterdir() if p.is_dir())
    if baseline not in names:
        return [{"pair": "AgentDiff", "error": f"no `{baseline}` traces to compare against"}]
    results = []
    for name in names:
        if name == baseline:
            continue
        pair = f"{baseline}-vs-{name}"
        pdir = out_dir / pair
        tdir = pdir / "traces"
        if tdir.exists():
            shutil.rmtree(tdir)
        tdir.mkdir(parents=True)
        for side in (baseline, name):
            for f in (traces_dir / side).glob("*.json"):
                shutil.copy2(f, tdir / f.name)
        log(f"agentdiff runs: {pair}")
        runs = subprocess.run([binary, "runs", str(tdir), "-o", str(pdir / "runs")],
                              capture_output=True, text=True, timeout=900)
        ev = subprocess.run([binary, "eval", str(tdir), "-o", str(pdir / "eval")],
                            capture_output=True, text=True, timeout=900)
        (pdir / "agentdiff-runs.txt").write_text(runs.stdout + runs.stderr)
        (pdir / "agentdiff-eval.txt").write_text(ev.stdout + ev.stderr)
        if runs.returncode != 0:
            results.append({"pair": pair, "error": f"`agentdiff runs` exited {runs.returncode}: "
                            f"{(runs.stderr or runs.stdout).strip()[-600:]}", "out": str(pdir)})
            continue
        keep = [l for l in runs.stdout.splitlines()
                if l.strip() and not l.startswith(("warning", "Wrote "))]
        rel = lambda p: p.relative_to(out_dir.parent).as_posix()  # noqa: E731
        results.append({"pair": pair, "summary": "\n".join(keep[:40]), "out": rel(pdir / "runs"),
                        "eval": rel(pdir / "eval")})
    return results


def write_reports(results: list[dict], out_dir: str | Path, meta: dict,
                  agentdiff: list[dict] | None = None) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"date": time.strftime("%Y-%m-%d %H:%M"), **meta}
    js, md = out / "results.json", out / "report.md"
    js.write_text(json.dumps({"meta": meta, "summary": summarize(results), "paired": paired(results),
                              "agentdiff": agentdiff or [], "results": results}, indent=2))
    md.write_text(markdown_report(results, meta, agentdiff))
    return js, md


def merge_runs(dirs: list[str | Path], out_dir: str | Path) -> list[dict]:
    """Combine the runs.jsonl logs and traces of several benchmark outputs (shards, or resumed pieces)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict] = {}
    for d in map(Path, dirs):
        merged.update(load_runs(d / "runs.jsonl"))
        if (d / "traces").is_dir():
            shutil.copytree(d / "traces", out / "traces", dirs_exist_ok=True)
    results = sorted(merged.values(), key=lambda r: (r.get("repeat", 0), r["strategy"], r["task"]))
    (out / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results))
    return results
