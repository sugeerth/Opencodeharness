"""Verification: run a check command, and keep the agent from passing by editing the checks."""
import hashlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    passed: bool
    exit_code: int
    output: str
    seconds: float


def run_check(command: str, cwd: str | Path, timeout: int = 600) -> CheckResult:
    """Run `command` in a shell in `cwd`; exit 0 passes. Output is the combined stdout/stderr tail."""
    start = time.monotonic()
    proc = subprocess.Popen(command, shell=True, cwd=cwd, text=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        out += f"\n[check timed out after {timeout}s]"
        code = -1
    return CheckResult(code == 0, code, out[-8000:], time.monotonic() - start)


class Guard:
    """Snapshot files matching glob patterns so edits to them can be detected and undone.

    Restoring before each check means a passing check is always run against the original tests.
    New files that match a pattern are left alone; only changed or deleted ones are restored.
    """

    def __init__(self, root: str | Path, patterns: list[str]):
        self.root = Path(root)
        self.patterns = list(patterns)
        self.saved: dict[str, bytes] = {}
        for rel in self._matches():
            self.saved[rel] = (self.root / rel).read_bytes()

    def _matches(self) -> list[str]:
        found = set()
        for pat in self.patterns:
            for p in self.root.glob(pat):
                # Before Python 3.13 a trailing "**" matches only directories, so expand them.
                for f in (p.rglob("*") if p.is_dir() else [p]):
                    rel = f.relative_to(self.root)
                    if f.is_file() and not f.is_symlink() and ".git" not in rel.parts:
                        found.add(rel.as_posix())
        return sorted(found)

    def tampered(self) -> list[str]:
        bad = []
        for rel, data in self.saved.items():
            p = self.root / rel
            if not p.is_file() or _digest(p.read_bytes()) != _digest(data):
                bad.append(rel)
        return bad

    def restore(self) -> list[str]:
        """Put changed or deleted protected files back; return which ones were touched."""
        bad = self.tampered()
        for rel in bad:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.saved[rel])
        return bad


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
