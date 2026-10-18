"""Verification: run a check command, and keep the agent from passing by editing the checks."""
import hashlib
import os
import re
import shutil
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


def run_check(command: str, cwd: str | Path, timeout: int = 600, sandbox=None) -> CheckResult:
    """Run `command` in a shell in `cwd` (inside the sandbox's container if given); exit 0 passes.
    Output is the combined stdout/stderr tail."""
    start = time.monotonic()
    container = None
    if sandbox is not None:
        import uuid
        container = f"openagent-check-{uuid.uuid4().hex[:12]}"
        proc = subprocess.Popen(sandbox.wrap_shell(command, Path(cwd), container), cwd=cwd, text=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    else:
        proc = subprocess.Popen(command, shell=True, cwd=cwd, text=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        if container:
            sandbox.kill(container)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a descendant in another session still holds the pipe
            proc.stdout.close()
            proc.wait()
            out = ""
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
                    # Caches (e.g. __pycache__) are rewritten by running the check itself, not by the agent.
                    if (f.is_file() and not f.is_symlink() and not SKIP_DIRS.intersection(rel.parts)
                            and f.suffix not in CACHE_SUFFIXES):
                        found.add(rel.as_posix())
        return sorted(found)

    def tampered(self) -> list[str]:
        bad = []
        for rel, data in self.saved.items():
            p = self.root / rel
            # A symlink in place of a protected file is tampering, whatever it points at.
            if p.is_symlink() or not p.is_file() or _digest(p.read_bytes()) != _digest(data):
                bad.append(rel)
        return bad

    def restore(self) -> list[str]:
        """Put changed or deleted protected files back; return which ones were touched."""
        bad = self.tampered()
        for rel in bad:
            p = self.root / rel
            # Never write through a link the agent put there: that would overwrite its target.
            if p.is_symlink():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.saved[rel])
        return bad


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Lines worth showing the agent from a failing check, wherever they are in the output.
_SIGNAL = re.compile(r"FAIL|ERROR|Error|error[:\[]|Traceback|assert|Assert|[Ee]xpected|panic|"
                     r"not ok|✗|✕|×|exception|Exception|undefined|cannot|Cannot|No such file")


def excerpt(output: str, max_lines: int = 120, tail: int = 30) -> str:
    """The parts of a check's output that explain the failure: lines that look like errors or
    assertions (with a line of context each side), then the tail, de-duplicated, in order."""
    lines = output.rstrip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    keep: set[int] = set(range(max(0, len(lines) - tail), len(lines)))
    for i, line in enumerate(lines):
        if _SIGNAL.search(line):
            keep.update(range(max(0, i - 1), min(len(lines), i + 2)))
    idx = sorted(keep)
    if len(idx) > max_lines:  # too many signal lines: keep the first ones and the tail
        tail_idx = idx[-tail:]
        idx = sorted(set(idx[: max_lines - tail]) | set(tail_idx))
    out, prev = [], -1
    for i in idx:
        if i != prev + 1:
            out.append(f"... [{i - prev - 1} lines omitted]")
        out.append(lines[i])
        prev = i
    if prev < len(lines) - 1:
        out.append(f"... [{len(lines) - 1 - prev} lines omitted]")
    return "\n".join(out)


CACHE_SUFFIXES = {".pyc", ".pyo"}
# Skipped at any depth: version control, dependencies and caches nobody edits by hand.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
             ".next", ".opencode"}
CACHE_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def fingerprint(root: str | Path, max_files: int = 50_000) -> dict[str, str]:
    """{relative path: content digest} for the workspace, skipping VCS, dependency and cache dirs.
    Used to see what a round changed and to notice rounds that changed nothing."""
    root = Path(root)
    out: dict[str, str] = {}
    ignored = _git_ignored_dirs(root)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        # Beyond the fixed list, skip what git ignores (build output, environments) or, outside a git
        # repository, virtualenvs. A directory merely *named* build/ or target/ may hold source.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and (here / d).relative_to(root).as_posix() not in ignored
                             and not (here / d / "pyvenv.cfg").is_file())
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            try:
                st = p.stat()
                out[p.relative_to(root).as_posix()] = (
                    _digest(p.read_bytes()) if st.st_size <= 4_000_000 else f"{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                continue
            if len(out) >= max_files:
                return out
    return out


def _git_ignored_dirs(root: Path) -> set[str]:
    if not (root / ".git").exists():
        return set()
    try:
        p = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--ignored", "--exclude-standard",
                            "--directory", "-z"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {e.rstrip("/") for e in p.stdout.split("\0") if e.endswith("/")} if p.returncode == 0 else set()


def changes(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {"added": sorted(set(after) - set(before)),
            "modified": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
            "deleted": sorted(set(before) - set(after))}
