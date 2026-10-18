"""`openagent evolve`: a harness that improves itself on your tasks, and keeps a change only when the
runs say it helped.

Each generation:
  1. Read the current harness's runs for signals: runs that passed the visible check but failed the
     held-out grade, runs that stalled, ran out of rounds, or edited protected files.
  2. Let the strongest signal name one change from a small library (an instruction added to the
     prompt, or a knob such as more repair rounds or the failing output up front). No model proposes changes; every proposal is a
     rule over counts, and the ledger says which signal and which tasks proposed it.
  3. Run the candidate on every task and compare it with the current harness task by task. Keep it
     only if it wins more tasks than it loses and loses none (AgentDiff's rule for a harness change).
  4. Run the tasks it moved again, under both harnesses, with fresh runs. A win that does not hold
     on the re-run is reverted.

The current harness's runs are carried from one generation to the next, so each generation costs
one candidate arm plus the re-run of the tasks it moved.
"""
import json
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .agent import Strategy
from .bench import agentdiff_eval, run_bench, sign_test
from .opencode import OpenCode
from .tasks import Task
from .traces import safe_name

Log = Callable[[str], None]


@dataclass(frozen=True)
class Candidate:
    id: str
    signal: str                   # the signal that proposes it
    why: str                      # what the signal means, for the ledger
    instruction: str = ""         # added to the prompt's rules, or
    rounds: int = 0               # added to the repair rounds, or
    show_failure: bool = False    # the failing check output goes in the first prompt


LIBRARY = [
    Candidate("edge_cases", "held_out_fail",
              "runs passed the visible check but failed the held-out grade: the check doesn't cover everything the task asks",
              instruction="Before you finish, list every requirement and edge case the task states, including limits "
                          "on performance such as time complexity, and make sure the code meets each one, not only the "
                          "ones the tests check."),
    Candidate("write_tests", "held_out_fail",
              "runs passed the visible check but failed the held-out grade: with the grading tests held out, the "
              "agent has to test the change itself",
              instruction="Before you finish, write tests for exactly what the task asks, edge cases included, run "
                          "them with the project's test command, and fix what fails."),
    Candidate("reread_failure", "stalled",
              "runs stalled: the agent stopped making edits while the check still failed",
              instruction="When the check still fails, re-read the failing assertion and the test that makes it before "
                          "you answer, find the input it uses, and change the code; never conclude the test or the "
                          "environment is wrong."),
    Candidate("respect_tests", "tampered",
              "runs edited protected files: the agent tried to pass by changing the tests",
              instruction="The tests are the specification. Never edit, skip or relax a test, even one that looks too "
                          "strict; change the code until it passes."),
    Candidate("read_tests_first", "check_failed",
              "runs ended with the check still failing",
              instruction="Read the tests for the code you change before editing it, and run them yourself before you "
                          "finish."),
    Candidate("show_failure", "check_failed",
              "runs ended with the check still failing: start the agent from the failure instead of letting it search",
              show_failure=True),
    Candidate("more_rounds", "rounds_exhausted",
              "runs used every repair round and were still failing", rounds=2),
]


def signals(results: list[dict]) -> dict[str, list[str]]:
    """{signal: [tasks it fired on]} over a harness's run records."""
    out: dict[str, set[str]] = {}

    def add(sig: str, task: str):
        out.setdefault(sig, set()).add(task)

    for r in results:
        t = r["task"]
        if r.get("graded") is False and r.get("check_passed"):
            add("held_out_fail", t)
        stop = r.get("stop")
        if stop == "stalled":
            add("stalled", t)
        if stop == "rounds_exhausted":
            add("rounds_exhausted", t)
        if not r.get("check_passed", r.get("passed")) and stop not in ("infra_error", "error", "setup_failed"):
            add("check_failed", t)
        if any(rd.get("tampered") for a in r.get("attempts", []) for rd in a.get("rounds", [])):
            add("tampered", t)
    return {k: sorted(v) for k, v in out.items()}


def propose(sigs: dict[str, list[str]], tried: set[str], strategy: Strategy) -> list[tuple[Candidate, list[str]]]:
    """Untried candidates whose signal fired, strongest signal first (more tasks), library order on ties."""
    order = {c.id: i for i, c in enumerate(LIBRARY)}
    picks = [(c, sigs[c.signal]) for c in LIBRARY
             if c.signal in sigs and c.id not in tried and c.instruction not in strategy.instructions
             and not (c.show_failure and strategy.show_failure)]
    return sorted(picks, key=lambda p: (-len(p[1]), order[p[0].id]))


def apply(strategy: Strategy, c: Candidate, name: str) -> Strategy:
    return replace(strategy, name=name, instructions=strategy.instructions + ((c.instruction,) if c.instruction else ()),
                   rounds=strategy.rounds + c.rounds, show_failure=strategy.show_failure or c.show_failure)


def rates(results: list[dict]) -> dict[str, float]:
    by: dict[str, list[bool]] = {}
    for r in results:
        by.setdefault(r["task"], []).append(bool(r["passed"]))
    return {t: sum(v) / len(v) for t, v in by.items()}


def compare(current: dict[str, float], candidate: dict[str, float], tasks: list[str] | None = None) -> dict:
    tasks = tasks if tasks is not None else sorted(set(current) & set(candidate))
    won = [t for t in tasks if candidate.get(t, 0) > current.get(t, 0)]
    lost = [t for t in tasks if candidate.get(t, 0) < current.get(t, 0)]
    return {"wins": won, "losses": lost, "ties": [t for t in tasks if t not in won and t not in lost],
            "sign_test_p": sign_test(len(won), len(lost))}


def _keep(cmp: dict) -> bool:
    return len(cmp["wins"]) > len(cmp["losses"]) and not cmp["losses"]


def evolve(tasks: list[Task], base: Strategy, oc: OpenCode, out_dir: str | Path, generations: int = 5,
           repeat: int = 1, jobs: int = 1, log: Log = lambda _: None, synthetic: str | None = None,
           use_agentdiff: bool = False) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    current = replace(base, name="v0")
    ledger: dict = {"started": time.strftime("%Y-%m-%d %H:%M"), "model": oc.model, "tasks": [t.name for t in tasks],
                    "repeat": repeat, "base": base.to_dict(), "synthetic": synthetic, "generations": []}

    def bench(strategies, gen_dir, only=None, rep=repeat):
        ts = [t for t in tasks if only is None or t.name in only]
        return run_bench(ts, strategies, oc, rep, jobs, log, gen_dir / "traces", synthetic)

    log(f"generation 0: running the starting harness on {len(tasks)} task(s)")
    cur_results = bench([current], out / "gen-0")
    cur_dir = out / "gen-0" / "traces" / safe_name(current.name)
    tried: set[str] = set()
    history = [{"version": current.name, "harness": current.to_dict(), "rates": rates(cur_results)}]

    for g in range(1, generations + 1):
        cur_rates = rates(cur_results)
        if all(v == 1.0 for v in cur_rates.values()):
            ledger["stopped"] = "every task passes every run"
            break
        sigs = signals(cur_results)
        picks = propose(sigs, tried, current)
        if not picks:
            ledger["stopped"] = "no signal names an untried change" + (f" (signals: {', '.join(sigs)})" if sigs else "")
            break
        cand_spec, fired_on = picks[0]
        tried.add(cand_spec.id)
        name = f"v{g}"  # one name per generation, so a reverted candidate's name is never reused
        cand = apply(current, cand_spec, name)
        gen_dir = out / f"gen-{g}"
        log(f"generation {g}: trying {cand_spec.id} (signal {cand_spec.signal} on {', '.join(fired_on)})")
        cand_results = bench([cand], gen_dir)
        cmp = compare(cur_rates, rates(cand_results))
        entry = {"generation": g, "candidate": cand_spec.id, "name": name, "signal": cand_spec.signal,
                 "proposed_by": fired_on, "why": cand_spec.why, "instruction": cand_spec.instruction,
                 "rounds_added": cand_spec.rounds, "show_failure": cand_spec.show_failure, "current": current.name,
                 "rates": {"current": cur_rates, "candidate": rates(cand_results)}, **cmp}
        # carry the current harness's traces beside the candidate's, so AgentDiff can pair them
        shutil.copytree(cur_dir, gen_dir / "traces" / safe_name(current.name), dirs_exist_ok=True)
        if not _keep(cmp):
            entry["verdict"] = "reverted"
            entry["reason"] = ("it lost " + ", ".join(cmp["losses"]) if cmp["losses"]
                               else "it moved no task" if not cmp["wins"] else "not more wins than losses")
        else:
            moved = cmp["wins"] + cmp["losses"]
            log(f"generation {g}: {cand_spec.id} won {', '.join(cmp['wins'])}; running the moved task(s) again")
            confirm = bench([current, cand], gen_dir / "confirm", only=set(moved))
            c_cur = rates([r for r in confirm if r["strategy"] == current.name])
            c_cand = rates([r for r in confirm if r["strategy"] == cand.name])
            ccmp = compare(c_cur, c_cand, moved)
            entry["confirm"] = {"tasks": moved, "rates": {"current": c_cur, "candidate": c_cand}, **ccmp}
            if _keep(ccmp):
                entry["verdict"] = "kept"
                entry["reason"] = "won " + ", ".join(cmp["wins"]) + ", lost none, and held on the re-run"
                current, cur_results = cand, cand_results
                cur_dir = gen_dir / "traces" / safe_name(cand.name)
                history.append({"version": cand.name, "harness": cand.to_dict(), "rates": rates(cand_results)})
            else:
                entry["verdict"] = "reverted"
                entry["reason"] = "the win did not hold on the re-run"
        if use_agentdiff:
            entry["agentdiff"] = agentdiff_eval(gen_dir / "traces", gen_dir / "agentdiff", baseline=safe_name(entry["current"]),
                                                log=log)
        log(f"generation {g}: {cand_spec.id} {entry['verdict']} ({entry['reason']})")
        ledger["generations"].append(entry)
    else:
        ledger["stopped"] = f"reached {generations} generation(s)"

    final = replace(current, name="openagent-evolved")
    ledger["final"] = final.to_dict()
    ledger["history"] = history
    (out / "harness.json").write_text(json.dumps(final.to_dict(), indent=2) + "\n")
    (out / "evolution.json").write_text(json.dumps(ledger, indent=2))
    (out / "EVOLUTION.md").write_text(markdown(ledger))
    return ledger


def _pct(rs: dict[str, float]) -> str:
    if not rs:
        return "—"
    total = sum(rs.values())
    return f"{total:g}/{len(rs)}" if total == int(total) else f"{total:.2f}/{len(rs)}"


def markdown(ledger: dict) -> str:
    lines = ["# Open Agent: harness evolution", ""]
    if ledger.get("synthetic"):
        lines += [f"> **Synthetic run.** {ledger['synthetic']}", ""]
    hist = ledger.get("history", [])
    lines += [f"- Model: `{ledger.get('model') or 'OpenCode default'}`",
              f"- Tasks: {len(ledger['tasks'])}, runs per task per arm: {ledger['repeat']}",
              f"- Started: {ledger['started']}",
              f"- Stopped: {ledger.get('stopped', '')}", "",
              "A change is proposed by a signal in the current harness's runs, kept only if it wins more tasks than "
              "it loses and loses none, and then only if the tasks it moved go the same way when run again.", "",
              "## Lineage", "", "| Version | Tasks passing (pass rate summed over tasks) | Instructions added |",
              "|---|---|---|"]
    base_n = len(ledger["base"].get("instructions", []))
    for h in hist:
        added = h["harness"]["instructions"][base_n:]
        lines.append(f"| {h['version']} | {_pct(h['rates'])} | "
                     + ("; ".join(a[:80] + ("…" if len(a) > 80 else "") for a in added) or "—") + " |")
    lines += ["", "## Generations", ""]
    for e in ledger["generations"]:
        lines += [f"### Generation {e['generation']}: `{e['candidate']}`, {e['verdict']}", "",
                  f"- Proposed by **{e['signal']}** on {', '.join(e['proposed_by'])}: {e['why']}.",
                  f"- Change: " + (f"add the instruction “{e['instruction']}”" if e["instruction"]
                                   else "show the failing check output in the first prompt" if e.get("show_failure")
                                   else f"add {e['rounds_added']} repair round(s)"),
                  f"- Against {e['current']}: won {', '.join(e['wins']) or 'nothing'}; "
                  f"lost {', '.join(e['losses']) or 'nothing'}; {len(e['ties'])} tie(s)"
                  + (f"; sign test p = {e['sign_test_p']:.3f}" if e.get("sign_test_p") is not None else "")]
        if "confirm" in e:
            c = e["confirm"]
            lines.append(f"- Re-run of {', '.join(c['tasks'])}: won {', '.join(c['wins']) or 'nothing'}; "
                         f"lost {', '.join(c['losses']) or 'nothing'}")
        lines += [f"- Verdict: **{e['verdict']}**, {e['reason']}.", ""]
        tasks = sorted(e["rates"]["current"])
        lines += ["| Task | " + e["current"] + " | " + e["name"] + " |", "|---|---|---|"]
        for t in tasks:
            a, b = e["rates"]["current"].get(t, 0), e["rates"]["candidate"].get(t, 0)
            mark = " ▲" if b > a else " ▼" if b < a else ""
            lines.append(f"| {t} | {a:.0%} | {b:.0%}{mark} |")
        lines.append("")
        for a in e.get("agentdiff") or []:
            if a.get("summary"):
                lines += ["AgentDiff:", "```", a["summary"].strip().splitlines()[1] if len(a["summary"].splitlines()) > 1
                          else a["summary"].strip(), "```", ""]
    fin = ledger.get("final", {})
    lines += ["## Final harness", "", "`harness.json` (use it with `openagent run --harness harness.json`):", "",
              "```json", json.dumps(fin, indent=2), "```", ""]
    return "\n".join(lines)
