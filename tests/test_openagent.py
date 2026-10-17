import json
import os
import sys
from pathlib import Path

import pytest

from openagent.agent import PRESETS, Strategy, first_prompt, pick_winner, solve, sync_tree, Attempt
from openagent.bench import markdown_report, run_bench, summarize, write_reports
from openagent.cli import main
from openagent.events import parse_events, summarize as summarize_events
from openagent.opencode import OpenCode
from openagent.tasks import Task, discover
from openagent.verify import Guard, run_check

FAKE = Path(__file__).with_name("fake_opencode.py")
CHECK = "grep -qx hi hello.txt"
EXAMPLES = Path(__file__).parents[1] / "examples" / "tasks"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Return a factory for an OpenCode pointing at the fake binary in a given mode."""
    log = tmp_path / "calls.jsonl"
    exe = tmp_path / "opencode"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_LOG", str(log))

    def make(mode="good", **kw):
        monkeypatch.setenv("FAKE_MODE", mode)
        return OpenCode(binary=str(exe), **kw)

    make.calls = lambda: [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return make


def ws(tmp_path, name="ws"):
    d = tmp_path / name
    d.mkdir()
    return d


# --- events -------------------------------------------------------------------------------

def test_parse_and_summarize_events():
    out = "\n".join([
        "noise", "{not json",
        json.dumps({"type": "tool_use", "sessionID": "s1", "part": {"tool": "edit", "state": {"status": "completed"}}}),
        json.dumps({"type": "tool_use", "sessionID": "s1", "part": {"tool": "bash", "state": {"status": "error"}}}),
        json.dumps({"type": "step_finish", "sessionID": "s1",
                    "part": {"cost": 0.5, "tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 3, "write": 4}}}}),
        json.dumps({"type": "step_finish", "sessionID": "s1", "part": {"cost": None, "tokens": None}}),
        json.dumps({"type": "text", "sessionID": "s1", "part": {"text": "done"}}),
        json.dumps({"type": "error", "sessionID": "s1", "error": {"name": "X", "data": {"message": "boom"}}}),
    ])
    s = summarize_events(parse_events(out))
    assert s.session_id == "s1" and s.steps == 2 and s.tool_calls == 2 and s.tool_errors == 1
    assert s.tools == {"edit": 1, "bash": 1} and s.total_tokens == 20 and s.cost == 0.5
    assert s.last_text == "done" and s.errors == ["boom"]


# --- opencode -----------------------------------------------------------------------------

def test_command_shape():
    oc = OpenCode(model="anthropic/x", agent="build", extra_args=["--thinking"])
    cmd = oc.command("-dash prompt", session="ses_1")
    assert cmd[:5] == ["opencode", "run", "--format", "json", "--auto"]
    assert cmd[cmd.index("--model") + 1] == "anthropic/x" and cmd[cmd.index("--session") + 1] == "ses_1"
    assert "--thinking" in cmd and cmd[-1] == " -dash prompt"
    assert "--auto" not in OpenCode(auto=False).command("x")


def test_missing_binary(tmp_path):
    r = OpenCode(binary=str(tmp_path / "nope")).run("x", tmp_path)
    assert r.exit_code == 127 and "not found" in r.stats.errors[0]


def test_timeout_kills(tmp_path):
    exe = tmp_path / "slow"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    r = OpenCode(binary=str(exe), timeout=1).run("x", tmp_path)
    assert r.timed_out and not r.ok and r.seconds < 15


# --- verify -------------------------------------------------------------------------------

def test_run_check(tmp_path):
    assert run_check("true", tmp_path).passed
    r = run_check("echo nope; exit 3", tmp_path)
    assert not r.passed and r.exit_code == 3 and "nope" in r.output
    assert not run_check("sleep 5", tmp_path, timeout=1).passed


def test_guard_ignores_caches_the_check_writes(tmp_path):
    (tmp_path / "tests" / "__pycache__").mkdir(parents=True)
    (tmp_path / "tests" / "t.py").write_text("T")
    (tmp_path / "tests" / "__pycache__" / "t.cpython-313.pyc").write_bytes(b"old")
    g = Guard(tmp_path, ["tests/**"])
    (tmp_path / "tests" / "__pycache__" / "t.cpython-313.pyc").write_bytes(b"new")
    assert list(g.saved) == ["tests/t.py"] and g.restore() == []


def test_guard_restores_changed_and_deleted(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.py").write_text("A")
    (tmp_path / "tests" / "b.py").write_text("B")
    g = Guard(tmp_path, ["tests/**"])
    (tmp_path / "tests" / "a.py").write_text("hacked")
    (tmp_path / "tests" / "b.py").unlink()
    (tmp_path / "tests" / "new.py").write_text("new")
    assert sorted(g.restore()) == ["tests/a.py", "tests/b.py"]
    assert (tmp_path / "tests" / "a.py").read_text() == "A" and (tmp_path / "tests" / "b.py").read_text() == "B"
    assert (tmp_path / "tests" / "new.py").exists() and g.tampered() == []


# --- agent loop ---------------------------------------------------------------------------

def test_prompts():
    assert first_prompt("do x", "make test", PRESETS["baseline"]) == "do x"
    p = first_prompt("do x", "make test", PRESETS["openagent"])
    assert "autonomously" in p and "`make test`" in p and p.endswith("Task:\ndo x")


def test_passes_first_try(fake, tmp_path):
    d = ws(tmp_path)
    out = solve("t", d, CHECK, fake("good"), PRESETS["openagent"])
    assert out.passed and out.repairs == 0 and len(fake.calls()) == 1
    # OpenCode reads its project directory from $PWD, so it must point at the workspace
    assert fake.calls()[0]["pwd"] == str(d.resolve())
    assert out.stats.cost == pytest.approx(0.01) and out.stats.tool_calls == 2


def test_repair_continues_session(fake, tmp_path):
    out = solve("t", ws(tmp_path), CHECK, fake("repair"), PRESETS["openagent"])
    assert out.passed and out.repairs == 1
    calls = fake.calls()
    assert "--session" not in calls[0]["argv"]
    sid = calls[1]["argv"][calls[1]["argv"].index("--session") + 1]
    assert sid.startswith("ses_") and "Verification failed" in calls[1]["argv"][-1]
    assert out.stats.cost == pytest.approx(0.02)


def test_baseline_does_not_repair(fake, tmp_path):
    out = solve("t", ws(tmp_path), CHECK, fake("repair"), PRESETS["baseline"])
    assert not out.passed and len(fake.calls()) == 1
    assert fake.calls()[0]["argv"][-1] == "t"


def test_gives_up_after_rounds(fake, tmp_path):
    out = solve("t", ws(tmp_path), CHECK, fake("never"), Strategy(rounds=2))
    assert not out.passed and len(out.attempts[0].rounds) == 3 and len(fake.calls()) == 3


def test_crash_without_check_fails(fake, tmp_path):
    out = solve("t", ws(tmp_path), None, fake("crash"), PRESETS["openagent"])
    assert not out.passed and out.attempts[0].rounds[0].stats.errors == ["model unavailable"]


def test_provider_error_stops_instead_of_repairing(fake, tmp_path):
    out = solve("t", ws(tmp_path), CHECK, fake("crash"), Strategy(rounds=3))
    assert not out.passed and len(fake.calls()) == 1


def test_missing_binary_stops_early(tmp_path):
    out = solve("t", ws(tmp_path), CHECK, OpenCode(binary=str(tmp_path / "nope")), Strategy(rounds=3))
    assert not out.passed and len(out.attempts[0].rounds) == 1


def test_protected_files_restored_and_reported(fake, tmp_path):
    d = ws(tmp_path)
    (d / "tests").mkdir()
    (d / "tests" / "check.txt").write_text("hi\n")
    out = solve("t", d, "diff -q hello.txt tests/check.txt", fake("cheat"), Strategy(rounds=1), protect=["tests/**"])
    assert not out.passed
    assert (d / "tests" / "check.txt").read_text() == "hi\n"
    assert out.attempts[0].rounds[0].tampered == ["tests/check.txt"]
    assert "protected files" in fake.calls()[1]["argv"][-1]


def test_best_of_n_keeps_passing_attempt(fake, tmp_path):
    d = ws(tmp_path)
    (d / "keep.txt").write_text("k")
    (d / "stale.txt").write_text("s")
    out = solve("t", d, CHECK, fake("flaky"), Strategy(rounds=0, attempts=3))
    # attempt-0 and attempt-2 succeed; attempt-1 doesn't. Tie broken by index.
    assert out.passed and out.winner == 0 and [a.passed for a in out.attempts] == [True, False, True]
    assert (d / "hello.txt").read_text() == "hi\n" and (d / "keep.txt").read_text() == "k"
    assert {Path(c["cwd"]).name for c in fake.calls()} == {"attempt-0", "attempt-1", "attempt-2"}


def test_best_of_n_all_fail_leaves_workdir_untouched(fake, tmp_path):
    d = ws(tmp_path)
    out = solve("t", d, CHECK, fake("never"), Strategy(rounds=0, attempts=2))
    assert not out.passed and out.winner is None and not (d / "hello.txt").exists()


def test_pick_winner_prefers_fewer_rounds():
    from openagent.agent import Round
    from openagent.events import RunStats
    r = lambda: Round(0, 1.0, RunStats(cost=0.1), None, [])  # noqa: E731
    assert pick_winner([Attempt(True, [r(), r()]), Attempt(True, [r()]), Attempt(False, [r()])]) == 1
    assert pick_winner([Attempt(False, [r()])]) is None


def test_sync_tree(tmp_path):
    src, dst = ws(tmp_path, "src"), ws(tmp_path, "dst")
    (src / "a").mkdir()
    (src / "a" / "f.txt").write_text("new")
    (src / "link").symlink_to("a/f.txt")
    (dst / "a").mkdir()
    (dst / "a" / "f.txt").write_text("old")
    (dst / "gone.txt").write_text("x")
    (dst / "gone_dir").mkdir()
    (dst / ".git").mkdir()
    (dst / ".git" / "HEAD").write_text("ref")
    sync_tree(src, dst)
    assert (dst / "a" / "f.txt").read_text() == "new" and os.readlink(dst / "link") == "a/f.txt"
    assert not (dst / "gone.txt").exists() and not (dst / "gone_dir").exists()
    assert (dst / ".git" / "HEAD").read_text() == "ref"


# --- tasks & bench -------------------------------------------------------------------------

def test_discover_examples():
    tasks = {t.name: t for t in discover(EXAMPLES)}
    assert {"hello", "fix-average", "slugify", "lru-cache"} <= set(tasks)
    assert tasks["fix-average"].protect == ["tests/**"] and "unittest" in tasks["fix-average"].check
    assert tasks["hello"].check == "grep -qx hi hello.txt"


def test_bench_and_report(fake, tmp_path):
    tasks = [Task.load(EXAMPLES / "hello")]
    results = run_bench(tasks, [PRESETS["baseline"], PRESETS["openagent"]], fake("repair"), repeat=2, jobs=2)
    rows = {r["strategy"]: r for r in summarize(results)}
    assert rows["baseline"]["passed"] == 0 and rows["openagent"]["passed"] == 2
    assert rows["openagent"]["avg_repairs"] == 1
    md = markdown_report(results, {"tasks": 1, "repeat": 2, "date": "today"})
    assert "+100 pts" in md and "| hello | 0/2 | 2/2 |" in md
    js, mdp = write_reports(results, tmp_path / "out", {"tasks": 1})
    assert json.loads(js.read_text())["summary"] and mdp.exists()


def test_bench_setup_failure(fake, tmp_path):
    t = Task("broken", "x", setup="exit 1", check=CHECK)
    (r,) = run_bench([t], [PRESETS["openagent"]], fake("good"))
    assert not r["passed"] and r["error"].startswith("setup failed")


# --- cli ----------------------------------------------------------------------------------

def test_cli_run(fake, tmp_path, capsys):
    oc = fake("repair")
    d = ws(tmp_path)
    rec = tmp_path / "run.json"
    code = main(["run", "say hi", "-C", str(d), "-c", CHECK, "--binary", oc.binary, "--json", str(rec)])
    assert code == 0 and json.loads(rec.read_text())["repairs"] == 1
    assert "wrote hi" in capsys.readouterr().out


def test_cli_run_failure_exit_code(fake, tmp_path):
    oc = fake("never")
    assert main(["run", "x", "-C", str(ws(tmp_path)), "-c", CHECK, "--binary", oc.binary, "--rounds", "1"]) == 1


def test_cli_bench(fake, tmp_path, capsys):
    oc = fake("good")
    code = main(["bench", str(EXAMPLES), "--only", "hello", "--binary", oc.binary, "-o", str(tmp_path / "r")])
    assert code == 0 and (tmp_path / "r" / "report.md").exists()
    out = capsys.readouterr().out
    assert "baseline" in out and "openagent" in out


def test_cli_demo_without_tasks(tmp_path, fake):
    oc = fake("good")
    assert main(["demo", "--tasks", str(tmp_path / "missing"), "--binary", oc.binary]) == 2


def test_cli_presets(capsys):
    assert main(["presets"]) == 0 and "best-of-3" in capsys.readouterr().out


# --- v0.3: loop details ----------------------------------------------------------------------

def test_excerpt_keeps_errors_and_tail():
    from openagent.verify import excerpt
    out = "\n".join([f"noise {i}" for i in range(300)] + ["FAIL: test_x", "AssertionError: 1 != 2"]
                    + [f"tail {i}" for i in range(5)])
    ex = excerpt(out, max_lines=40, tail=10)
    assert "FAIL: test_x" in ex and "tail 4" in ex and "lines omitted" in ex and "noise 5" not in ex
    assert excerpt("short\noutput") == "short\noutput"


def test_stalls_when_rounds_change_nothing(fake, tmp_path):
    d = ws(tmp_path)
    (d / "hello.txt").write_text("bye\n")  # FAKE_MODE=never writes "bye": no change at all
    out = solve("t", d, CHECK, fake("never"), Strategy(rounds=5))
    a = out.attempts[0]
    assert a.stop == "stalled" and len(a.rounds) == 2 and len(fake.calls()) == 2
    assert "changed no files" in fake.calls()[1]["argv"][-1]


def test_preflight_rejects_unrunnable_check(fake, tmp_path):
    out = solve("t", ws(tmp_path), "no-such-command-xyz --flag", fake("good"), PRESETS["openagent"])
    assert not out.passed and "could not run" in out.error and fake.calls() == []


def test_preflight_notes_already_passing(fake, tmp_path):
    out = solve("t", ws(tmp_path), "true", fake("good"), PRESETS["openagent"])
    assert out.passed and out.to_dict()["already_passing"]


def test_cost_budget_stops(fake, tmp_path):
    out = solve("t", ws(tmp_path), CHECK, fake("never"), Strategy(rounds=5), max_cost=0.005)
    assert out.attempts[0].stop == "budget" and len(fake.calls()) == 1


def test_changes_recorded(fake, tmp_path):
    d = ws(tmp_path)
    (d / "old.txt").write_text("x")
    out = solve("t", d, CHECK, fake("good"), PRESETS["openagent"])
    assert out.best.changes["added"] == ["hello.txt"] and out.attempts[0].rounds[0].changed == ["hello.txt"]


def test_stop_reasons(fake, tmp_path):
    assert solve("t", ws(tmp_path, "a"), CHECK, fake("good")).best.stop == "passed"
    assert solve("t", ws(tmp_path, "b"), CHECK, fake("crash")).best.stop == "infra_error"


# --- traces for AgentDiff ------------------------------------------------------------------------

def _trajectory(fake, tmp_path, mode="repair", **kw):
    from openagent.traces import to_trajectory
    out = solve("say hi", ws(tmp_path), CHECK, fake(mode), PRESETS["openagent"])
    return out, to_trajectory(out, "hello", "say hi", CHECK, 1, "m/x", **kw)


def test_trajectory_shape(fake, tmp_path):
    out, t = _trajectory(fake, tmp_path)
    names = [s["name"] for s in t["steps"]]
    assert t["schema_version"] == 1 and t["trace_id"] == "hello__openagent__r1"
    assert names.count("openagent.check") == 2 and names.count("openagent.repair") == 1
    assert t["steps"][-1]["type"] == "answer" and [s["index"] for s in t["steps"]] == list(range(len(t["steps"])))
    assert t["outcome"]["success"] and t["totals"]["cost_usd"] == pytest.approx(0.02)
    tokens = sum(s.get("tokens", 0) for s in t["steps"])
    assert tokens == out.stats.total_tokens
    assert {s["type"] for s in t["steps"]} <= {"plan", "search", "retrieve", "read", "tool_call", "reason", "answer"}


def test_trajectory_uses_held_out_grade(fake, tmp_path):
    _, t = _trajectory(fake, tmp_path, mode="good", graded=False, grade_output="hidden test failed",
                       synthetic="scripted")
    assert t["outcome"]["success"] is False and "held-out grade failed" in t["outcome"]["answer"]
    assert t["harness"]["graded_by"] == "grade" and t["harness"]["note"].startswith("SYNTHETIC")


def test_trajectory_validates_with_agentdiff(fake, tmp_path):
    trace = pytest.importorskip("agentdiff.trace")
    _, t = _trajectory(fake, tmp_path)
    traj = trace.Trajectory.from_dict(t)
    assert traj.outcome.success and traj.steps[-1].type == "answer" and traj.outcome.termination == "agent_stop"
    process = pytest.importorskip("agentdiff.process")
    write = next(st for st in traj.steps if st.name == "write")
    assert process.effect_of(write) == ("write", "declared") and process.is_error(write) == (False, "declared")


def test_trace_names_are_safe():
    from openagent.traces import safe_name
    assert safe_name("best-of-3") == "best-of-3" and "__" not in safe_name("a__b c")


# --- bench: grading and the keep rule -------------------------------------------------------------

def test_bench_held_out_grade_and_traces(fake, tmp_path):
    t = Task("g", "x", check=CHECK, grade="false")
    (r,) = run_bench([t], [PRESETS["openagent"]], fake("good"), traces_dir=tmp_path / "traces")
    assert r["check_passed"] and r["graded"] is False and r["passed"] is False
    assert (tmp_path / r["trace"]).is_file() and r["trace"].startswith("traces/openagent/")


def test_task_placeholder(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "prompt.txt").write_text("p")
    (d / "task.json").write_text(json.dumps({"check": "test -f x", "grade": "bash {task}/hidden.sh"}))
    t = Task.load(d)
    assert t.grade == f"bash {d.resolve()}/hidden.sh"


def test_paired_keep_rule_and_sign_test():
    from openagent.bench import paired, sign_test
    rows = [{"task": t, "strategy": s, "passed": p} for t, s, p in [
        ("a", "baseline", False), ("a", "x", True), ("b", "baseline", True), ("b", "x", True),
        ("a", "y", True), ("b", "y", False)]]
    pr = {r["strategy"]: r for r in paired(rows)}
    assert pr["x"]["verdict"] == "keep" and pr["x"]["wins"] == ["a"]
    assert pr["y"]["verdict"].startswith("no difference") or pr["y"]["verdict"] == "revert"
    assert sign_test(0, 0) is None and sign_test(10, 0) == pytest.approx(2 / 1024)


# --- the scripted model ------------------------------------------------------------------------

def _policy(tmp_path):
    from openagent.sim import Playbook, Policy
    d = tmp_path / "task"
    (d / "sim").mkdir(parents=True)
    (d / "prompt.txt").write_text('Make "it" work\nplease.')
    (d / "sim" / "fix.py").write_text("fixed")
    (d / "sim.json").write_text(json.dumps({
        "first": [{"read": "a.py"}, {"say": "first done"}],
        "verify": [{"bash": "make test"}, {"say": "verified"}],
        "repair": [[{"write": "a.py", "from": "sim/fix.py"}, {"say": "repaired"}]]}))
    return Policy([Playbook.load(d)])


def _body(*msgs):
    system = {"role": "system", "content": "<env>\n  Working directory: /w\n</env>"}
    return {"tools": [{}], "messages": [system, *msgs]}


def test_policy_first_turn_decodes_opencode_strings(tmp_path):
    p = _policy(tmp_path)
    user = {"role": "user", "content": json.dumps('Make "it" work\nplease.').replace("\\n", "\n")}
    assert p.respond(_body(user)) == {"tool": "read", "args": {"filePath": "/w/a.py"}}
    after = _body(user, {"role": "assistant", "content": ""}, {"role": "tool", "content": "..."})
    assert p.respond(after) == {"text": "first done"}


def test_policy_guided_turn_runs_verify_steps(tmp_path):
    from openagent.sim import GUIDANCE_MARK
    p = _policy(tmp_path)
    user = {"role": "user", "content": f"- {GUIDANCE_MARK} and fix.\n\nTask:\nMake \"it\" work\nplease."}
    a = {"role": "assistant", "content": ""}
    assert p.respond(_body(user, a))["tool"] == "bash"  # the closing "say" gave way to verify
    assert p.respond(_body(user, a, a)) == {"text": "verified"}


def test_policy_repair_turn(tmp_path):
    p = _policy(tmp_path)
    msgs = [{"role": "user", "content": 'Make "it" work\nplease.'}, {"role": "assistant", "content": "x"},
            {"role": "user", "content": "Verification failed (repair 1 of 3). ..."}]
    assert p.respond(_body(*msgs)) == {"tool": "write", "args": {"filePath": "/w/a.py", "content": "fixed"}}
    assert p.respond({"messages": msgs}) == {"text": "Scripted run"}  # title request: no tools


# --- end to end with the real OpenCode (skipped where it isn't installed) -------------------------

@pytest.mark.skipif(not __import__("shutil").which("opencode"), reason="OpenCode is not installed")
def test_demo_end_to_end_with_real_opencode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # a relative output directory: the scripted model's config path must still resolve inside workspaces
    code = main(["demo", "--only", "hello", "--only", "fix-average", "--preset", "baseline",
                 "--preset", "openagent", "--timeout", "120", "-o", "demo"])
    res = json.loads((tmp_path / "demo" / "results.json").read_text())
    got = {(r["task"], r["strategy"]): r["passed"] for r in res["results"]}
    assert code == 0 and got == {("hello", "baseline"): True, ("hello", "openagent"): True,
                                 ("fix-average", "baseline"): False, ("fix-average", "openagent"): True}
    assert list(tmp_path.iterdir()) == [tmp_path / "demo"]  # nothing leaked outside the workspaces
