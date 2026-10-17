import stat

from opencodeharness.cli import main
from opencodeharness.opencode import Task, build_command, parse_events, run_task


def fake_binary(tmp_path, body):
    b = tmp_path / "fake-opencode"
    b.write_text("#!/bin/bash\n" + body)
    b.chmod(b.stat().st_mode | stat.S_IEXEC)
    return str(b)


def test_build_command(tmp_path):
    cmd = build_command("do it", tmp_path, model="anthropic/x", agent="build")
    assert cmd[:3] == ["opencode", "run", "--format"] and cmd[-1] == "do it"
    assert "--model" in cmd and "--agent" in cmd


def test_parse_events_skips_noise():
    assert parse_events('{"a":1}\nnot json\n{"b":2}') == [{"a": 1}, {"b": 2}]


def test_run_task_pass_and_fail(tmp_path):
    check = tmp_path / "check.sh"
    check.write_text("grep -qx hi hello.txt")
    t = Task("t", "p", check=check)
    ok = fake_binary(tmp_path, 'while [ $# -gt 0 ]; do [ "$1" = --dir ] && d=$2; shift; done; echo hi > "$d/hello.txt"; echo \'{"type":"x"}\'')
    r = run_task(t, binary=ok)
    assert r.passed and len(r.events) == 1
    assert not run_task(t, binary=fake_binary(tmp_path, "exit 0")).passed


def test_missing_binary(tmp_path):
    assert "not found" in run_task(Task("t", "p"), binary="/nonexistent/oc").detail


def test_cli_exit_code(tmp_path):
    d = tmp_path / "tasks" / "a"
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("x")
    assert main([str(tmp_path / "tasks"), "--binary", fake_binary(tmp_path, "exit 0")]) == 0
