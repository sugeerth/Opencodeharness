"""Drive the OpenCode CLI (`opencode run`) against task directories and check the results."""
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Task:
    """A task is a directory: `prompt.txt`, optional `files/` (starting workspace), optional `check.sh`.

    `check.sh` runs in the workspace after the agent finishes; exit 0 means pass.
    """
    name: str
    prompt: str
    files: Path | None = None
    check: Path | None = None

    @classmethod
    def load(cls, d: Path) -> "Task":
        return cls(d.name, (d / "prompt.txt").read_text().strip(),
                   d / "files" if (d / "files").is_dir() else None,
                   d / "check.sh" if (d / "check.sh").is_file() else None)


@dataclass
class Result:
    task: str
    passed: bool
    exit_code: int
    seconds: float = 0.0
    events: list = field(default_factory=list)
    detail: str = ""


def build_command(prompt: str, workspace: Path, model: str | None = None, agent: str | None = None,
                  binary: str = "opencode") -> list[str]:
    cmd = [binary, "run", "--format", "json", "--auto", "--dir", str(workspace)]
    if model:
        cmd += ["--model", model]
    if agent:
        cmd += ["--agent", agent]
    return cmd + [prompt]


def parse_events(stdout: str) -> list[dict]:
    """`--format json` prints one JSON event per line; ignore anything else."""
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def run_task(task: Task, model: str | None = None, agent: str | None = None,
             timeout: int = 600, binary: str = "opencode") -> Result:
    import time
    with tempfile.TemporaryDirectory(prefix=f"och-{task.name}-") as tmp:
        ws = Path(tmp)
        if task.files:
            shutil.copytree(task.files, ws, dirs_exist_ok=True)
        start = time.monotonic()
        try:
            p = subprocess.run(build_command(task.prompt, ws, model, agent, binary),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Result(task.name, False, -1, timeout, detail=f"timed out after {timeout}s")
        except FileNotFoundError:
            return Result(task.name, False, -1, detail=f"`{binary}` not found; install OpenCode first")
        secs = time.monotonic() - start
        passed, detail = p.returncode == 0, p.stderr[-500:]
        if passed and task.check:
            c = subprocess.run(["bash", str(task.check.resolve())], cwd=ws, capture_output=True, text=True)
            passed, detail = c.returncode == 0, (c.stdout + c.stderr)[-500:]
        return Result(task.name, passed, p.returncode, secs, parse_events(p.stdout), detail)
