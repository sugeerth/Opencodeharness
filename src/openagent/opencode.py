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

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def infra_failure(self) -> bool:
        """OpenCode failed before the model did any work (missing binary, auth, network, bad model).
        Repair prompts can't fix that, so the loop should stop instead of retrying."""
        s = self.stats
        return not self.ok and not self.timed_out and s.steps == 0 and s.tool_calls == 0 and bool(s.errors)


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

    def command(self, prompt: str, session: str | None = None) -> list[str]:
        cmd = [self.binary, "run", "--format", "json"]
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
        env = {**os.environ, **(self.env or {})}
        try:
            proc = subprocess.Popen(self.command(prompt, session), cwd=cwd, env=env, text=True,
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
            _kill_group(proc)
            out, err = proc.communicate()
        stats = summarize(parse_events(out))
        if timed_out:
            stats.errors.append(f"timed out after {self.timeout}s")
        return RunResult(proc.returncode if not timed_out else -1, stats,
                         time.monotonic() - start, err[-4000:], timed_out)


def _kill_group(proc: subprocess.Popen) -> None:
    """OpenCode may spawn a server and tool subprocesses; take the whole group down."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
