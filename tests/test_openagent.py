import json
import os
import shutil
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


def test_relative_binary_resolves_from_where_openagent_started(fake, tmp_path, monkeypatch):
    oc = fake("good")
    monkeypatch.chdir(Path(oc.binary).parent)
    rel = OpenCode(binary="./" + Path(oc.binary).name)
    assert rel.binary == oc.binary and solve("t", ws(tmp_path), CHECK, rel).passed


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


def test_empty_run_is_retried_once(fake, tmp_path):
    out = solve("t", ws(tmp_path), CHECK, fake("silent-once"), Strategy(rounds=3))
    a = out.attempts[0]
    assert out.passed and len(fake.calls()) == 2 and a.rounds[0].retried and len(a.rounds) == 1


def test_empty_twice_is_infra_error(fake, tmp_path):
    out = solve("t", ws(tmp_path), CHECK, fake("silent"), Strategy(rounds=3))
    a = out.attempts[0]
    assert a.stop == "infra_error" and len(fake.calls()) == 2 and "no output twice" in a.rounds[0].stats.errors[0]


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


# --- v0.4: evolve ------------------------------------------------------------------------------

def test_signals_from_run_records():
    from openagent.evolve import signals
    recs = [
        {"task": "a", "passed": False, "check_passed": True, "graded": False, "stop": "passed", "attempts": []},
        {"task": "b", "passed": False, "check_passed": False, "stop": "stalled", "attempts": []},
        {"task": "c", "passed": False, "check_passed": False, "stop": "rounds_exhausted",
         "attempts": [{"rounds": [{"tampered": ["tests/x.py"]}]}]},
        {"task": "d", "passed": False, "check_passed": False, "stop": "infra_error", "attempts": []},
    ]
    assert signals(recs) == {"held_out_fail": ["a"], "stalled": ["b"], "check_failed": ["b", "c"],
                             "rounds_exhausted": ["c"], "tampered": ["c"]}


def test_propose_orders_by_signal_strength_and_skips_tried():
    from openagent.evolve import propose
    sigs = {"stalled": ["x"], "check_failed": ["x", "y"], "held_out_fail": ["z"]}
    ids = [c.id for c, _ in propose(sigs, set(), PRESETS["openagent"])]
    assert ids == ["read_tests_first", "show_failure", "edge_cases", "write_tests", "reread_failure"]
    assert "read_tests_first" not in [c.id for c, _ in propose(sigs, {"read_tests_first"}, PRESETS["openagent"])]


def test_keep_rule_and_compare():
    from openagent.evolve import _keep, compare
    assert _keep(compare({"a": 0, "b": 1}, {"a": 1, "b": 1}))
    assert not _keep(compare({"a": 0, "b": 1}, {"a": 1, "b": 0}))  # breaks a task
    assert not _keep(compare({"a": 1}, {"a": 1}))                  # moves nothing


def test_harness_file_round_trip(tmp_path):
    s = Strategy("h", rounds=5, instructions=("Mind every edge case.",))
    (tmp_path / "h.json").write_text(json.dumps(s.to_dict()))
    loaded = Strategy.load(tmp_path / "h.json")
    assert loaded == s and "- Mind every edge case." in first_prompt("t", None, loaded)
    assert "Rules:\n- x" in first_prompt("t", None, Strategy(preamble=False, instructions=("x",)))


def test_evolve_keeps_the_change_the_signal_names(fake, tmp_path):
    from openagent.evolve import evolve
    tasks = [Task("t", "write hello", check="test -f hello.txt", grade=CHECK)]
    ledger = evolve(tasks, PRESETS["openagent"], fake("edgy"), tmp_path / "evo", generations=3)
    (g1,) = ledger["generations"]
    assert g1["candidate"] == "edge_cases" and g1["signal"] == "held_out_fail" and g1["verdict"] == "kept"
    assert g1["confirm"]["wins"] == ["t"] and ledger["stopped"] == "every task passes every run"
    h = json.loads((tmp_path / "evo" / "harness.json").read_text())
    assert any("edge case" in i for i in h["instructions"]) and h["name"] == "openagent-evolved"
    assert "kept" in (tmp_path / "evo" / "EVOLUTION.md").read_text()
    # the current harness's traces are carried beside the candidate's for AgentDiff
    assert (tmp_path / "evo" / "gen-1" / "traces" / "v0").is_dir() and (tmp_path / "evo" / "gen-1" / "traces" / "v1").is_dir()


def test_evolve_reverts_and_stops_without_signal(fake, tmp_path):
    from openagent.evolve import evolve
    tasks = [Task("t", "write hello", check=CHECK)]
    ledger = evolve(tasks, PRESETS["openagent"], fake("never"), tmp_path / "evo", generations=5)
    verdicts = {g["candidate"]: g["verdict"] for g in ledger["generations"]}
    assert verdicts and set(verdicts.values()) == {"reverted"}
    assert ledger["stopped"].startswith("no signal names an untried change")
    assert json.loads((tmp_path / "evo" / "harness.json").read_text())["instructions"] == []


def test_cli_run_with_harness(fake, tmp_path):
    oc = fake("edgy")
    h = tmp_path / "h.json"
    h.write_text(json.dumps(Strategy("x", instructions=("Mind every edge case.",)).to_dict()))
    assert main(["run", "x", "-C", str(ws(tmp_path)), "-c", CHECK, "--binary", oc.binary, "--harness", str(h)]) == 0


# --- v0.4: tasks from git history ----------------------------------------------------------------

def _git_repo(tmp_path):
    import subprocess
    r = tmp_path / "repo"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_add.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    g("add", "-A"); g("commit", "-qm", "initial")
    # a real task: code and a new test that fails on the parent
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    (r / "tests" / "test_sub.py").write_text("from calc import sub\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
    g("add", "-A"); g("commit", "-qm", "Add sub()\n\nCo-Authored-By: x <x@example.com>")
    # not a task: its test already passes on the parent
    (r / "calc.py").write_text((r / "calc.py").read_text() + "\n# tidy\n")
    (r / "tests" / "test_add2.py").write_text("from calc import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n")
    g("add", "-A"); g("commit", "-qm", "Tidy and test add")
    # not a candidate: docs only
    (r / "README.md").write_text("calc\n")
    g("add", "-A"); g("commit", "-qm", "Docs")
    return r


TEST_CMD = f"{sys.executable} -m pytest -q -p no:cacheprovider {{tests}}"


def test_from_git_validates_and_writes_tasks(tmp_path):
    from openagent.gittasks import build
    report = build(_git_repo(tmp_path), tmp_path / "tasks", TEST_CMD)
    assert [t["subject"] for t in report["taken"]] == ["Add sub()"]
    assert [s["why"] for s in report["skipped"]] == ["its tests already pass on the parent: nothing to do"]
    (task,) = discover(tmp_path / "tasks")
    assert "Add sub()" in task.prompt and "Co-Authored-By" not in task.prompt and "not given to you" in task.prompt
    assert not (task.files / "tests" / "test_sub.py").exists()      # held out
    assert "def sub" not in (task.files / "calc.py").read_text()     # starts from the parent
    assert task.grade.startswith("cp -r ") and "test_sub.py" in task.grade
    assert (tmp_path / "tasks" / "from-git.json").is_file()


def test_from_git_grade_fails_on_parent_and_passes_with_the_fix(tmp_path):
    from openagent.gittasks import build
    build(_git_repo(tmp_path), tmp_path / "tasks", TEST_CMD)
    (task,) = discover(tmp_path / "tasks")
    import shutil as sh
    w = tmp_path / "w"
    sh.copytree(task.files, w)
    assert not run_check(task.grade, w).passed
    (w / "calc.py").write_text((w / "calc.py").read_text() + "\n\ndef sub(a, b):\n    return a - b\n")
    assert run_check(task.grade, w).passed


def test_from_git_reveal_tests(tmp_path):
    from openagent.gittasks import build
    build(_git_repo(tmp_path), tmp_path / "tasks", TEST_CMD, reveal=True)
    (task,) = discover(tmp_path / "tasks")
    assert (task.files / "tests" / "test_sub.py").exists() and task.grade is None
    assert "test_sub.py" in task.check and task.protect == ["tests/test_sub.py"]
    assert "already in the repository" in task.prompt


# --- v0.5: the web front end -------------------------------------------------------------------

@pytest.fixture
def ui(tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from openagent.ui.server import Store, make_handler
    store = Store(tmp_path / "uihome")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "tok"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, token="tok", host=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-OpenAgent-Token": token, "Content-Type": "application/json",
                                              **({"Host": host} if host else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    call.store = store
    yield call
    httpd.shutdown()


def test_ui_page_and_auth(ui):
    code, page = ui("/", token="")
    assert code == 200 and 'const TOKEN = "tok"' in page
    assert ui("/api/jobs", token="wrong")[0] == 403
    assert ui("/api/jobs", host="evil.example")[0] == 403            # DNS rebinding
    assert ui("/api/run", {"task": "x", "dir": "/"}, token="")[0] == 403  # CSRF
    assert ui("/api/info")[1]["presets"] == list(PRESETS)


def test_ui_run_job_end_to_end(ui, fake, tmp_path):
    import time as _t
    oc = fake("repair")
    repo = ws(tmp_path, "repo")
    (repo / "hello.txt").write_text("bye\n")
    code, job = ui("/api/run", {"dir": str(repo), "task": "say hi", "check": CHECK, "binary": oc.binary, "copy": True})
    assert code == 200
    for _ in range(100):
        code, j = ui(f"/api/jobs/{job['id']}")
        if j["status"] not in ("queued", "running"):
            break
        _t.sleep(0.1)
    assert j["status"] == "passed" and j["summary"]["repairs"] == 1 and j["summary"]["files"] == ["hello.txt"]
    assert any("check passed" in l for l in j["log"])
    assert (repo / "hello.txt").read_text() == "bye\n"            # worked in a copy
    diff = ui(f"/api/jobs/{job['id']}/file?path=diff.patch")[1]
    assert "-bye" in diff and "+hi" in diff
    trace = ui(f"/api/jobs/{job['id']}/file?path={j['summary']['trace']}")[1]
    assert trace["schema_version"] == 1 and trace["outcome"]["success"]
    assert ui(f"/api/jobs/{job['id']}/file?path=../../../etc/passwd")[0] == 403


def test_ui_import_and_delete_keeps_the_folder(ui):
    demo = Path(__file__).parents[1] / "docs" / "demo"
    code, job = ui("/api/import", {"dir": str(demo)})
    assert code == 200 and job["status"] == "done"
    res = ui(f"/api/jobs/{job['id']}/file?path=results.json")[1]
    assert {s["strategy"] for s in res["summary"]} >= {"baseline", "openagent"}
    assert ui("/api/import", {"dir": "/nonexistent"})[0] == 400
    assert ui(f"/api/jobs/{job['id']}/delete", {})[1] == {"ok": True}
    assert (demo / "results.json").is_file() and ui("/api/jobs")[1] == []


def test_ui_jobs_survive_a_restart(ui, tmp_path):
    from openagent.ui.server import Store
    ui("/api/import", {"dir": str(Path(__file__).parents[1] / "docs" / "evolve")})
    again = Store(ui.store.home)
    assert [j.kind for j in again.jobs.values()] == ["import"]


# --- v0.5: regressions from the independent review ------------------------------------------------

def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/usr/bin/env python3\n" + body)
    p.chmod(0o755)
    return str(p)


def test_timeout_kills_children_that_ignore_sigterm(tmp_path):
    import time as _t
    exe = tmp_path / "oc.sh"
    exe.write_text("#!/bin/bash\n(trap '' TERM; sleep 30) &\nsleep 100\n")
    exe.chmod(0o755)
    t0 = _t.monotonic()
    r = OpenCode(binary=str(exe), timeout=1).run("x", tmp_path)
    assert r.timed_out and _t.monotonic() - t0 < 15


def test_guard_never_writes_through_a_symlink(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("TEST")
    (tmp_path / "src" / "app.py").write_text("APP")
    g = Guard(tmp_path, ["tests/**"])
    (tmp_path / "tests" / "test_a.py").unlink()
    (tmp_path / "tests" / "test_a.py").symlink_to(tmp_path / "src" / "app.py")
    assert g.restore() == ["tests/test_a.py"]
    assert (tmp_path / "src" / "app.py").read_text() == "APP"
    assert not (tmp_path / "tests" / "test_a.py").is_symlink() and (tmp_path / "tests" / "test_a.py").read_text() == "TEST"


BUMP = ("import json, re\np = 'build/compiler.py'\nv = int(re.search(r'V = (\\d+)', open(p).read()).group(1)) + 1\n"
        "open(p, 'w').write(f'V = {v}\\n')\nprint(json.dumps({'type': 'step_finish', 'sessionID': 's', 'part': {'cost': 0}}))\n")


def test_edits_under_a_dir_named_build_are_seen(tmp_path):
    d = ws(tmp_path)
    (d / "build").mkdir()
    (d / "build" / "compiler.py").write_text("V = 0\n")
    out = solve("bump", d, "grep -q 'V = 3' build/compiler.py", OpenCode(binary=_script(tmp_path, "oc", BUMP)),
                Strategy(rounds=5))
    assert out.passed and out.best.changes["modified"] == ["build/compiler.py"]


def test_fingerprint_skips_what_git_ignores_and_virtualenvs(tmp_path):
    import subprocess
    from openagent.verify import fingerprint
    d = ws(tmp_path)
    subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
    (d / ".gitignore").write_text("out/\n")
    for rel in ("out/x.o", "src/a.py", "env/pyvenv.cfg", "env/lib.py"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x")
    assert sorted(fingerprint(d)) == [".gitignore", "src/a.py"]


def test_files_the_check_writes_are_not_agent_edits(fake, tmp_path):
    d = ws(tmp_path)
    (d / "hello.txt").write_text("bye\n")
    check = "date +%s%N > report.xml; grep -qx hi hello.txt"
    out = solve("t", d, check, fake("never"), Strategy(rounds=5))
    a = out.best
    assert a.stop == "stalled" and all("report.xml" not in r.changed for r in a.rounds)
    assert "report.xml" not in sum(a.changes.values(), [])


def test_best_of_n_keeps_relative_symlinks_outside_the_workspace(fake, tmp_path):
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "lib.txt").write_text("LIB")
    d = ws(tmp_path, "pkg")
    (d / "shared").symlink_to("../shared")
    out = solve("t", d, "test -f shared/lib.txt && grep -qx hi hello.txt", fake("good"), Strategy(rounds=0, attempts=2))
    assert out.passed and os.readlink(d / "shared") == "../shared"   # the user's link is left as it was


def test_best_of_n_reports_the_winner_and_nothing_applied_when_all_fail(fake, tmp_path):
    out = solve("t", ws(tmp_path, "a"), CHECK, fake("flaky"), Strategy(rounds=0, attempts=2))
    assert out.winner == 0 and out.final_text == "wrote hi"
    d = ws(tmp_path, "b")
    out = solve("t", d, CHECK, fake("never"), Strategy(rounds=0, attempts=2))
    assert out.winner is None and sum(out.applied_changes.values(), []) == [] and not (d / "hello.txt").exists()
    assert out.to_dict()["changes"] == out.applied_changes


def test_passing_run_is_not_labelled_timeout(tmp_path):
    from openagent.agent import Outcome, Round
    from openagent.events import RunStats
    from openagent.traces import to_trajectory
    from openagent.verify import CheckResult
    r0 = Round(-1, 1.0, RunStats(errors=["timed out after 1s"]), CheckResult(False, 1, "", 0), [])
    r1 = Round(0, 1.0, RunStats(), CheckResult(True, 0, "", 0), [])
    out = Outcome(PRESETS["openagent"], True, 0, [Attempt(True, [r0, r1], stop="passed")])
    assert to_trajectory(out, "t", "p", CHECK)["outcome"]["termination"] == "agent_stop"


def test_missing_directory_is_a_clear_error(fake, tmp_path):
    out = solve("t", tmp_path / "nope", CHECK, fake("good"))
    assert not out.passed and out.error.startswith("not a directory") and fake.calls() == []


def test_from_git_drops_old_versions_of_modified_tests_and_handles_unicode(tmp_path):
    import subprocess
    from openagent.gittasks import build
    r = tmp_path / "repo"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q"); g("config", "user.email", "t@e.com"); g("config", "user.name", "t")
    (r / "m.py").write_text("def f():\n    return 1\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 1\n")
    g("add", "-A"); g("commit", "-qm", "init")
    (r / "m.py").write_text("def f():\n    return 2\n")
    (r / "tests" / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n")
    (r / "tests" / "test_café.py").write_text("from m import f\n\ndef test_c():\n    assert f() == 2\n")
    g("add", "-A"); g("commit", "-qm", "f returns 2")
    report = build(r, tmp_path / "tasks", TEST_CMD)
    assert len(report["taken"]) == 1 and "tests/test_café.py" in report["taken"][0]["tests"]
    (task,) = discover(tmp_path / "tasks")
    assert not (task.files / "tests" / "test_m.py").exists()          # the old expectation is gone
    w = tmp_path / "w"
    shutil.copytree(task.files, w)
    (w / "m.py").write_text("def f():\n    return 2\n")                 # the correct change
    assert task.check is None                                           # nothing left to run: no check to fight it
    assert run_check(task.grade, w).passed


def test_setup_failure_is_not_an_agent_signal(fake, tmp_path):
    from openagent.evolve import signals
    (r,) = run_bench([Task("s", "x", setup="exit 1", check=CHECK)], [PRESETS["openagent"]], fake("good"))
    assert r["stop"] == "setup_failed" and signals([r]) == {}


def test_sim_prefers_the_most_specific_playbook(tmp_path):
    from openagent.sim import Playbook, Policy
    a, b = Playbook("Fix it", [{"say": "a"}]), Playbook("Fix it properly", [{"say": "b"}])
    assert Policy([a, b]).find(["Task:\nFix it properly"]) is b


# --- v0.6: scale -----------------------------------------------------------------------------------

def test_bench_resumes_from_runs_log(fake, tmp_path):
    tasks = [Task(f"t{i}", "x", check=CHECK) for i in range(3)]
    log = tmp_path / "runs.jsonl"
    first = run_bench(tasks, [PRESETS["openagent"]], fake("good"), runs_log=log)
    calls = len(fake.calls())
    log.write_text(log.read_text() + '{"task": "torn')            # killed mid-write
    again = run_bench(tasks, [PRESETS["openagent"]], fake("good"), runs_log=log)
    assert len(fake.calls()) == calls and [r["task"] for r in again] == [r["task"] for r in first]


def test_shards_partition_the_jobs_and_merge_back(fake, tmp_path):
    from openagent.bench import merge_runs
    tasks = [Task(f"t{i}", "x", check=CHECK) for i in range(5)]
    seen = []
    for i in (1, 2):
        out = tmp_path / f"s{i}"
        out.mkdir()
        rs = run_bench(tasks, [PRESETS["baseline"], PRESETS["openagent"]], fake("good"), shard=(i, 2),
                       runs_log=out / "runs.jsonl", traces_dir=out / "traces")
        seen += [(r["task"], r["strategy"]) for r in rs]
    assert len(seen) == 10 and len(set(seen)) == 10
    merged = merge_runs([tmp_path / "s1", tmp_path / "s2"], tmp_path / "m")
    assert len(merged) == 10 and len(list((tmp_path / "m" / "traces").rglob("*.json"))) == 10


def test_cli_merge_writes_one_report(fake, tmp_path):
    oc = fake("good")
    for i in (1, 2):
        main(["bench", str(EXAMPLES), "--only", "hello", "--only", "fix-average", "--binary", oc.binary,
              "--shard", f"{i}/2", "-o", str(tmp_path / f"s{i}")])
    assert main(["merge", str(tmp_path / "s1"), str(tmp_path / "s2"), "-o", str(tmp_path / "m")]) == 0
    res = json.loads((tmp_path / "m" / "results.json").read_text())
    assert len(res["results"]) == 4 and {r["runs"] for r in res["summary"]} == {2}


def test_attach_command_names_the_workspace(tmp_path):
    cmd = OpenCode(attach="http://127.0.0.1:4096").command("go", None, tmp_path)
    assert cmd[cmd.index("--attach") + 1] == "http://127.0.0.1:4096"
    assert cmd[cmd.index("--dir") + 1] == str(tmp_path.resolve()) and cmd[-1] == "go"
    assert "--attach" not in OpenCode().command("go", None, tmp_path)


def test_manifest_export_and_fetch_round_trip(tmp_path):
    from openagent.gittasks import build, export_manifest, fetch
    repo = _git_repo(tmp_path)
    build(repo, tmp_path / "mined", TEST_CMD)
    m = export_manifest([tmp_path / "mined"], "tiny")
    assert m["tasks"] == 1 and m["repos"][0]["url"] == str(repo)
    report = fetch(m, tmp_path / "fetched")
    assert len(report["written"]) == 1 and report["failed"] == []
    (task,) = discover(tmp_path / "fetched")
    assert "Add sub()" in task.prompt and task.grade and not (task.files / "tests" / "test_sub.py").exists()
    m["repos"][0]["tasks"].append({"sha": "0" * 40, "task": "ghost", "subject": "x", "tests": []})
    report = fetch(m, tmp_path / "fetched2")
    assert [f["task"] for f in report["failed"]] == ["ghost"]


def test_realworld_manifest_is_well_formed():
    import re
    m = json.loads((Path(__file__).parents[1] / "benchmarks" / "realworld-v1.json").read_text())
    tasks = [t for r in m["repos"] for t in r["tasks"]]
    assert m["format"] == "openagent-manifest/1" and m["tasks"] == len(tasks) >= 50
    assert len({t["task"] for t in tasks}) == len(tasks)
    assert all(re.fullmatch(r"[0-9a-f]{40}", t["sha"]) and t["tests"] for t in tasks)
    assert all(r["url"].startswith("https://github.com/") and "{tests}" in r["test_cmd"] for r in m["repos"])


def test_bench_accepts_a_manifest(fake, tmp_path):
    from openagent.gittasks import build, export_manifest
    build(_git_repo(tmp_path), tmp_path / "mined", TEST_CMD)
    (tmp_path / "m.json").write_text(json.dumps(export_manifest([tmp_path / "mined"])))
    oc = fake("good")
    assert main(["bench", str(tmp_path / "m.json"), "--binary", oc.binary, "-o", str(tmp_path / "out")]) == 0
    res = json.loads((tmp_path / "out" / "results.json").read_text())
    assert res["meta"]["tasks"] == 1 and (tmp_path / "out" / "tasks" / "fetch.json").is_file()
    assert all(r["graded"] is False for r in res["results"])   # the fake never adds sub(); the hidden tests say so


def test_from_git_drops_a_visible_check_that_fails_from_the_start(tmp_path):
    import subprocess
    from openagent.gittasks import build
    r = tmp_path / "repo"
    r.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q"); g("config", "user.email", "t@e.com"); g("config", "user.name", "t")
    (r / "m.py").write_text("def f():\n    return 1\n")
    (r / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 1\n")
    g("add", "-A"); g("commit", "-qm", "init")
    (r / "m.py").write_text("def f():\n    return 2\n")
    (r / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n")
    g("add", "-A"); g("commit", "-qm", "f returns 2")
    build(r, tmp_path / "tasks", TEST_CMD)       # the only test file is held out: nothing left to run
    (task,) = discover(tmp_path / "tasks")
    cfg = json.loads((tmp_path / "tasks" / task.name / "task.json").read_text())
    assert task.check is None and "exit 5" in cfg["check_dropped"] and task.grade


# --- v0.7: the Docker sandbox ----------------------------------------------------------------------

def _docker_ok():
    try:
        from openagent.sandbox import available
        import subprocess as sp
        return not available() and sp.run(["docker", "image", "inspect", "openagent-runner:latest"],
                                           capture_output=True).returncode == 0
    except Exception:
        return False


def test_sandbox_command_mounts_only_the_workspace_and_named_files(tmp_path, monkeypatch):
    from openagent.sandbox import Docker
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("UNRELATED_SECRET", "s")
    cmd = Docker(image="img").wrap(["opencode", "run", "x"], tmp_path / "ws", {"OPENCODE_CONFIG": str(cfg)}, "n1")
    assert cmd[:4] == ["docker", "run", "--rm", "--name"] and cmd[-4:] == ["img", "opencode", "run", "x"]
    assert f"{(tmp_path / 'ws').resolve()}:/work" in cmd and f"{cfg}:{cfg}:ro" in cmd
    assert "ANTHROPIC_API_KEY=k" in cmd and not any("UNRELATED_SECRET" in c for c in cmd)
    with pytest.raises(ValueError):
        Docker.parse("podman")
    assert Docker.parse("docker:my/img:1").image == "my/img:1"
    with pytest.raises(ValueError):
        OpenCode(sandbox=Docker(), attach="http://x")


@pytest.mark.skipif(not _docker_ok(), reason="Docker or the openagent-runner image is not available")
def test_sandbox_contains_writes_and_cleans_up_on_timeout(tmp_path):
    import subprocess as sp
    import uuid as _uuid
    from openagent.sandbox import Docker
    marker = f"/tmp/openagent-escape-{_uuid.uuid4().hex}"
    d = ws(tmp_path)
    r = run_check(f"touch {marker} && echo inside > /work/seen.txt", d, 60, Docker())
    assert r.passed and (d / "seen.txt").read_text() == "inside\n" and not Path(marker).exists()
    r = run_check("sleep 100", d, 3, Docker())
    assert not r.passed and "timed out" in r.output
    names = sp.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    assert "openagent-check-" not in names


@pytest.mark.skipif(not _docker_ok(), reason="Docker or the openagent-runner image is not available")
def test_sandbox_grades_with_the_held_out_tests_without_showing_them_to_the_agent(tmp_path):
    from openagent.gittasks import build
    from openagent.sandbox import Docker
    build(_git_repo(tmp_path), tmp_path / "tasks", "python3 -m pytest -q -p no:cacheprovider {tests}")
    (task,) = discover(tmp_path / "tasks")
    w = tmp_path / "w"
    shutil.copytree(task.files, w)
    r = run_check(task.grade, w, 120, Docker())
    assert not r.passed and "cannot stat" not in r.output            # fails on the tests, not on the copy
    (w / "calc.py").write_text((w / "calc.py").read_text() + "\n\ndef sub(a, b):\n    return a - b\n")
    assert run_check(task.grade, w, 120, Docker()).passed
    grade_dir = str(tmp_path / "tasks" / task.name / "grade")
    agent_cmd = Docker().wrap(["opencode", "run", f"look in {grade_dir}"], w)
    assert not any(grade_dir in a and a.endswith(":ro") for a in agent_cmd)


# --- summary, sim, GitHub Action ----------------------------------------------------------

def test_summary_markdown(fake, tmp_path):
    from openagent.summary import markdown
    out = solve("make hello say hi", ws(tmp_path), CHECK, fake("repair"), PRESETS["openagent"])
    md = markdown(out, "make hello say hi", CHECK)
    assert md.startswith("## Open Agent: ✅ the check passed")
    assert "> make hello say hi" in md and "| Repairs | 1 |" in md
    assert "| 1 |" in md and "fail (exit 1)" in md and "| 2 |" in md and "pass |" in md
    failed = solve("t", ws(tmp_path, "w2"), CHECK, fake("never"), Strategy(rounds=1))
    md = markdown(failed, "t", CHECK)
    assert "❌ the check did not pass" in md and "Last check output" in md and "the repair rounds ran out" in md


def test_run_writes_summary(fake, tmp_path):
    exe = fake("repair").binary
    d = ws(tmp_path)
    assert main(["run", "say hi", "-C", str(d), "-c", CHECK, "--binary", exe,
                 "--summary", str(tmp_path / "s.md")]) == 0
    assert "the check passed" in (tmp_path / "s.md").read_text()


def test_action_outputs(tmp_path):
    from openagent.action_outputs import outputs
    r = tmp_path / "r.json"
    r.write_text(json.dumps({"passed": True, "stop": "passed", "stats": {"cost": 0.25},
                             "changes": {"added": ["a"], "modified": ["b", "c"], "deleted": []}}))
    assert outputs(str(r)) == {"passed": "true", "stop": "passed", "changed": "3", "cost": "0.2500"}
    assert outputs(str(tmp_path / "missing.json")) == {"passed": "false", "stop": "error", "changed": "0",
                                                        "cost": "0.0000"}


def test_model_defaults_to_env(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAGENT_MODEL", "prov/m")
    exe = fake("good").binary
    assert main(["run", "say hi", "-C", str(ws(tmp_path)), "-c", CHECK, "--binary", exe]) == 0
    argv = fake.calls()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "prov/m"


def test_sim_command_serves_and_writes_env(tmp_path):
    import subprocess
    import time
    import urllib.request
    env_file = tmp_path / "sim.env"
    p = subprocess.Popen([sys.executable, "-m", "openagent.cli", "sim", str(EXAMPLES), "--env-file", str(env_file),
                          "--config-dir", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if env_file.exists() and env_file.read_text():
                break
            time.sleep(0.1)
        env = dict(l.split("=", 1) for l in env_file.read_text().splitlines())
        assert env["OPENAGENT_MODEL"] == "scripted/coder"
        config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        base = config["provider"]["scripted"]["options"]["baseURL"]
        with urllib.request.urlopen(base + "/models", timeout=5) as r:
            assert r.status == 200
    finally:
        p.terminate()
        assert p.wait(timeout=10) == 0


def test_github_action_runs_end_to_end(fake, tmp_path):
    """The composite action's own scripts, run the way a runner runs them, with the fake OpenCode."""
    yaml = pytest.importorskip("yaml")
    import re
    import subprocess
    root = Path(__file__).parents[1]
    spec = yaml.safe_load((root / "action.yml").read_text())
    exe = Path(fake("repair").binary)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # the action calls `opencode` and `openagent` from PATH
    (bindir / "opencode").symlink_to(exe)
    (bindir / "openagent").write_text(f"#!/bin/sh\nexec {sys.executable} -m openagent.cli \"$@\"\n")
    (bindir / "openagent").chmod(0o755)
    work = ws(tmp_path)
    inputs = {k: str(v.get("default", "")) for k, v in spec["inputs"].items()}
    inputs.update({"task": "say hi; it's \"quoted\" $(rm -rf /) `x`", "check": CHECK, "protect": "a/**\nb/**"})
    files = {n: tmp_path / n for n in ("output", "path", "summary")}
    for f in files.values():
        f.write_text("")
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_ACTION_PATH=str(root), GITHUB_RUN_ID="7",
               GITHUB_RUN_ATTEMPT="1", GITHUB_OUTPUT=str(files["output"]), GITHUB_PATH=str(files["path"]),
               GITHUB_STEP_SUMMARY=str(files["summary"]), PATH=f"{bindir}:{os.environ['PATH']}",
               PYTHONPATH=str(root / "src"))
    run = next(s for s in spec["runs"]["steps"] if s.get("id") == "run")
    sub = lambda s: re.sub(r"\$\{\{\s*inputs\.([\w-]+)\s*\}\}", lambda m: inputs[m.group(1)], str(s))
    step_env = dict(env, **{k: sub(v) for k, v in run["env"].items()})
    p = subprocess.run(["bash", "-e", "-c", run["run"]], cwd=work, env=step_env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    out = dict(l.split("=", 1) for l in files["output"].read_text().splitlines())
    assert out["passed"] == "true" and out["stop"] == "passed" and out["changed"] == "1"
    assert "the check passed" in files["summary"].read_text()
    argv = fake.calls()[0]["argv"]
    # the task reached OpenCode verbatim, never through the shell
    assert "$(rm -rf /)" in argv[-1] and '"quoted"' in argv[-1]
    assert Path(out["result"]).is_file() and list(Path(out["trace"]).glob("*.json"))


def test_mcp_server_solves_over_stdio(fake, tmp_path):
    import io
    from openagent.mcp import serve
    d = ws(tmp_path)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "solve", "arguments": {"task": "say hi", "directory": str(d), "check": CHECK}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "check", "arguments": {"command": "exit 3", "directory": str(d)}}},
            {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
    out = io.StringIO()
    serve(fake("repair"), io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n"), out)
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, 3, 4, 5, None]   # no reply to the notification
    assert replies[0]["result"]["serverInfo"]["name"] == "openagent"
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"solve", "check", "presets"}
    solved = replies[2]["result"]
    assert not solved["isError"] and "the check passed" in solved["content"][0]["text"]
    assert solved["structuredContent"]["passed"] and solved["structuredContent"]["repairs"] == 1
    assert (d / "hello.txt").read_text().strip() == "hi"
    assert "failed (exit 3)" in replies[3]["result"]["content"][0]["text"]
    assert replies[4]["error"]["code"] == -32601 and replies[5]["error"]["code"] == -32700


def test_doctor(tmp_path, monkeypatch):
    from openagent import doctor
    oc = tmp_path / "oc"
    oc.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 1.0.0; exit 0; fi\n"
                  "if [ \"$1\" = models ]; then echo prov/a; echo prov/b; exit 0; fi\nexit 1\n")
    oc.chmod(0o755)
    c = doctor.check_opencode(str(oc))
    assert c.status == "warn" and "1.0.0" in c.detail and doctor.TESTED_OPENCODE in c.fix
    assert doctor.check_opencode(str(tmp_path / "missing")).status == "fail"
    assert doctor.check_model(str(oc), "prov/b").status == "ok"
    bad = doctor.check_model(str(oc), "prov/zzz")
    assert bad.status == "fail" and "prov/a" in bad.fix
    assert doctor.check_model(str(oc), None).status == "skip"
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in list(os.environ):
        if k.endswith(doctor.KEY_SUFFIXES) or k == "OPENCODE_CONFIG":
            monkeypatch.delenv(k)
    assert doctor.check_credentials().status == "warn"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    c = doctor.check_credentials()
    assert c.status == "ok" and "ANTHROPIC_API_KEY" in c.detail and "secret" not in c.detail
    text = doctor.render([c, bad])
    assert "✗ model" in text and "→ try one of" in text and text.endswith("1 problem(s), 0 warning(s)")


def test_show_failure_puts_the_failing_output_in_the_first_prompt(fake, tmp_path):
    from openagent.agent import Strategy as S
    d = ws(tmp_path)
    (d / "hello.txt").write_text("bye\n")
    out = solve("t", d, CHECK + " || (echo 'AssertionError: expected hi' && false)", fake("good"),
                S(show_failure=True))
    assert out.passed
    first = fake.calls()[0]["argv"][-1]
    assert "Right now it fails (exit 1)" in first and "AssertionError: expected hi" in first
    # off by default, and round-trips through a harness file
    assert "Right now it fails" not in first_prompt("t", CHECK, PRESETS["openagent"])
    assert S.from_dict(S(show_failure=True).to_dict()).show_failure


def test_evolve_proposes_show_failure_once():
    from openagent.evolve import apply, propose, LIBRARY
    sigs = {"check_failed": ["a", "b"]}
    ids = [c.id for c, _ in propose(sigs, set(), PRESETS["openagent"])]
    assert "show_failure" in ids
    c = next(c for c in LIBRARY if c.id == "show_failure")
    s = apply(PRESETS["openagent"], c, "v1")
    assert s.show_failure and s.instructions == PRESETS["openagent"].instructions
    assert "show_failure" not in [c.id for c, _ in propose(sigs, set(), s)]
