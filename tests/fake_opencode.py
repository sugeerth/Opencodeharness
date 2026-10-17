"""A stand-in for `opencode run --format json` used by the tests.

Behaviour is chosen by FAKE_MODE:
  good    write hello.txt = "hi" on the first run
  repair  write "bye" on a fresh run, "hi" when continuing a session (so one repair fixes it)
  never   always write "bye"
  cheat   write "bye" and overwrite tests/check.txt (a protected file)
  crash   print an error event and exit 1
  flaky   attempt directories whose name ends in an even digit succeed; others never do
Every invocation is appended to $FAKE_LOG as a JSON line {"argv", "cwd"}.
"""
import json
import os
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "run" and "--format" in args, args
session = args[args.index("--session") + 1] if "--session" in args else None
cwd = Path.cwd()
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"argv": args, "cwd": str(cwd), "pwd": os.environ.get("PWD")}) + "\n")

sid = session or f"ses_{uuid.uuid4().hex[:8]}"


def emit(kind, **data):
    print(json.dumps({"type": kind, "timestamp": 0, "sessionID": sid, **data}), flush=True)


mode = os.environ.get("FAKE_MODE", "good")
emit("step_start", part={"type": "step-start"})
if mode == "crash":
    emit("error", error={"name": "ProviderError", "data": {"message": "model unavailable"}})
    sys.exit(1)
if mode == "good" or (mode == "repair" and session):
    content = "hi"
elif mode == "flaky":
    content = "hi" if cwd.name[-1:] in "02468" and cwd.name[-1:] != "" else "bye"
else:
    content = "bye"
(cwd / "hello.txt").write_text(content + "\n")
if mode == "cheat":
    (cwd / "tests").mkdir(exist_ok=True)
    (cwd / "tests" / "check.txt").write_text("hacked\n")
print("some non-json log line")
emit("tool_use", part={"tool": "write", "state": {"status": "completed"}})
emit("tool_use", part={"tool": "bash", "state": {"status": "error"}})
emit("text", part={"text": f"wrote {content}"})
emit("step_finish", part={"cost": 0.01, "tokens": {"input": 100, "output": 20, "reasoning": 5,
                                                    "cache": {"read": 50, "write": 10}}})
