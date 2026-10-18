"""Run the OpenCode CLI non-interactively and collect what happened."""
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .events import RunStats, parse_events, summarize


@dataclass
class RunResult:
    exit_code: int
    stats: RunStats
    seconds: float
    stderr: str = ""
    timed_out: bool = False
    events: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def empty(self) -> bool:
        """OpenCode printed no events at all: not even the start of a step. Whatever the agent would
        have done, it never got to; that is the platform failing, and worth one retry."""
        return not self.events and not self.timed_out

    @property
    def infra_failure(self) -> bool:
        """OpenCode failed before the model did any work (missing binary, auth, network, bad model).
        Repair prompts can't fix that, so the loop should stop instead of retrying."""
        s = self.stats
        return ((not self.ok or self.empty) and not self.timed_out and s.steps == 0 and s.tool_calls == 0
                and bool(s.errors))


@dataclass
class OpenCode:
    """How to invoke `opencode run`. The process runs with the workspace as its working directory,
    which works across OpenCode versions (newer builds dropped `--dir`)."""
    binary: str = "opencode"
    model: str | None = None          # provider/model, e.g. anthropic/claude-sonnet-5-5
    agent: str | None = None          # OpenCode agent name, e.g. build
    auto: bool = True                 # --auto: approve permissions that aren't explicitly denied
    extra_args: list[str] = field(default_factory=list)
    timeout: int = 900
    env: dict[str, str] | None = None
    attach: str | None = None         # URL of a running `opencode serve` to reuse instead of a cold start

    def __post_init__(self):
        # OpenCode runs inside each workspace, so a relative path like ./bin/opencode must be resolved
        # against the directory openagent was started from, now.
        if os.sep in self.binary and not os.path.isabs(self.binary):
            self.binary = os.path.abspath(self.binary)

    def command(self, prompt: str, session: str | None = None, cwd: str | Path | None = None) -> list[str]:
        cmd = [self.binary, "run", "--format", "json"]
        if self.attach:
            # with a shared server the workspace has to be named; there is no process cwd to inherit
            cmd += ["--attach", self.attach] + (["--dir", str(Path(cwd).resolve())] if cwd else [])
        if self.auto:
            cmd.append("--auto")
        if self.model:
            cmd += ["--model", self.model]
        if self.agent:
            cmd += ["--agent", self.agent]
        if session:
            cmd += ["--session", session]
        # A leading "-" would be parsed as a flag; a leading space keeps it a positional message.
        return cmd + self.extra_args + [" " + prompt if prompt.startswith("-") else prompt]

    def run(self, prompt: str, cwd: str | Path, session: str | None = None) -> RunResult:
        start = time.monotonic()
        # OpenCode takes its project directory from $PWD, which Popen's cwd= does not update.
        env = {**os.environ, **(self.env or {}), "PWD": str(Path(cwd).resolve())}
        try:
            proc = subprocess.Popen(self.command(prompt, session, cwd), cwd=cwd, env=env, text=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True)
        except FileNotFoundError:
            return RunResult(127, RunStats(errors=[f"`{self.binary}` not found; install OpenCode "
                                                   "(https://opencode.ai) or pass --binary"]), 0.0)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            out, err = _kill_group(proc)
        events = parse_events(out)
        stats = summarize(events)
        if timed_out:
            stats.errors.append(f"timed out after {self.timeout}s")
        return RunResult(proc.returncode if not timed_out else -1, stats,
                         time.monotonic() - start, err[-4000:], timed_out, events)


def _kill_group(proc: subprocess.Popen, grace: float = 5.0) -> tuple[str, str]:
    """Take the whole process group down (OpenCode may spawn a server and tool subprocesses) and
    return what it printed. SIGKILL always follows the grace period: the leader exiting on SIGTERM
    says nothing about children that ignore it and keep the output pipes open. If something outside
    the group still holds the pipes, stop reading rather than hang."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        return proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        proc.wait()
        return "", ""


class SharedServer:
    """`with SharedServer(oc) as url:` runs one `opencode serve` for many runs to attach to, which saves
    OpenCode's start-up on every run (about a second each, measured against the scripted model)."""

    def __init__(self, oc: "OpenCode", wait: float = 60.0):
        self.oc, self.wait, self.proc, self.url = oc, wait, None, None

    def __enter__(self) -> str:
        import re
        import threading
        env = {**os.environ, **(self.oc.env or {})}
        self.proc = subprocess.Popen([self.oc.binary, "serve", "--port", "0", "--print-logs"], env=env, text=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        found = threading.Event()

        def read():
            for line in self.proc.stdout:  # keep draining so the server never blocks on a full pipe
                m = re.search(r"(https?://[\w.\[\]:-]+)", line)
                if m and not found.is_set() and "listening" in line.lower():
                    self.url = m.group(1)
                    found.set()
        threading.Thread(target=read, daemon=True).start()
        if not found.wait(self.wait):
            self.__exit__()
            raise RuntimeError("`opencode serve` did not report a listening address")
        return self.url

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            _kill_group(self.proc, grace=3)
