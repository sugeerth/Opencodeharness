"""Open Agent: wrap OpenCode in a verify -> repair loop, with optional best-of-N attempts.

One attempt:
  1. Send the task to OpenCode (with autonomy guidance and the check command, unless disabled).
  2. Restore any protected files the agent changed, then run the check.
  3. If it fails, continue the same OpenCode session with the failure output and repeat,
     up to `rounds` repairs.

Best-of-N runs several attempts in parallel in separate copies of the workspace and keeps the
passing one that cost least.
"""
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .events import RunStats
from .opencode import OpenCode
from .verify import CheckResult, Guard, run_check

PREAMBLE = """You are working autonomously in this repository. No human will answer questions, so make reasonable assumptions and keep going.
- Start by reading the code relevant to the task; do not guess at structure.
- Make the smallest change that fully solves the task, matching the existing style.
- Before you finish, run the relevant tests or checks yourself and fix any failures.
- Never weaken, skip or delete tests or checks to make them pass."""

CHECK_HINT = "Your work will be verified by running `{check}` in the repository root. It must exit 0."

REPAIR = """Verification failed (repair {n} of {total}). `{check}` exited {code}. Output (last lines):
```
{output}
```
{tamper}Find the root cause and fix the code. Do not modify the tests or checks."""

REPAIR_NEW_SESSION = "You are continuing an earlier attempt at this task:\n\n{task}\n\n"

TAMPER = "Note: you changed protected files ({files}); they were restored before checking.\n"


@dataclass(frozen=True)
class Strategy:
    name: str = "openagent"
    preamble: bool = True        # prepend autonomy guidance
    reveal_check: bool = True    # tell the agent the check command
    rounds: int = 3              # repair rounds after the first try
    attempts: int = 1            # independent parallel attempts (best-of-N)


PRESETS = {
    "baseline": Strategy("baseline", preamble=False, reveal_check=False, rounds=0, attempts=1),
    "guided": Strategy("guided", rounds=0),
    "openagent": Strategy("openagent"),
    "best-of-3": Strategy("best-of-3", attempts=3),
}


@dataclass
class Round:
    exit_code: int
    seconds: float
    stats: RunStats
    check: CheckResult | None
    tampered: list[str]

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "seconds": round(self.seconds, 2), "stats": self.stats.to_dict(),
                "check": None if self.check is None else {"passed": self.check.passed, "exit_code": self.check.exit_code,
                                                          "seconds": round(self.check.seconds, 2)},
                "tampered": self.tampered}


@dataclass
class Attempt:
    passed: bool
    rounds: list[Round] = field(default_factory=list)
    workspace: Path | None = None

    @property
    def stats(self) -> RunStats:
        total = RunStats()
        for r in self.rounds:
            total.merge(r.stats)
        return total

    @property
    def seconds(self) -> float:
        return sum(r.seconds + (r.check.seconds if r.check else 0) for r in self.rounds)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "seconds": round(self.seconds, 2), "stats": self.stats.to_dict(),
                "rounds": [r.to_dict() for r in self.rounds]}


@dataclass
class Outcome:
    strategy: Strategy
    passed: bool
    winner: int | None
    attempts: list[Attempt]

    @property
    def stats(self) -> RunStats:
        total = RunStats()
        for a in self.attempts:
            total.merge(a.stats)
        return total

    @property
    def seconds(self) -> float:
        # Attempts run in parallel, so wall time is the slowest one.
        return max((a.seconds for a in self.attempts), default=0.0)

    @property
    def repairs(self) -> int:
        a = self.attempts[self.winner] if self.winner is not None else self.attempts[0]
        return max(len(a.rounds) - 1, 0)

    def to_dict(self) -> dict:
        s = self.strategy
        return {"strategy": s.name, "passed": self.passed, "winner": self.winner, "repairs": self.repairs,
                "seconds": round(self.seconds, 2), "stats": self.stats.to_dict(),
                "attempts": [a.to_dict() for a in self.attempts]}


Log = Callable[[str], None]


def first_prompt(task: str, check: str | None, strategy: Strategy) -> str:
    parts = []
    if strategy.preamble:
        parts.append(PREAMBLE)
    if check and strategy.reveal_check:
        parts.append(CHECK_HINT.format(check=check))
    parts.append(("Task:\n" if parts else "") + task)
    return "\n\n".join(parts)


def repair_prompt(task: str, check: str, result: CheckResult, n: int, total: int,
                  tampered: list[str], new_session: bool) -> str:
    output = "\n".join(result.output.strip().splitlines()[-80:])
    msg = REPAIR.format(n=n, total=total, check=check, code=result.exit_code, output=output,
                        tamper=TAMPER.format(files=", ".join(tampered)) if tampered else "")
    return (REPAIR_NEW_SESSION.format(task=task) if new_session else "") + msg


def attempt(task: str, workspace: Path, check: str | None, oc: OpenCode, strategy: Strategy,
            protect: list[str] = (), check_timeout: int = 600, log: Log = lambda _: None) -> Attempt:
    guard = Guard(workspace, list(protect)) if protect else None
    result = Attempt(passed=False, workspace=workspace)
    prompt, session = first_prompt(task, check, strategy), None
    for n in range(strategy.rounds + 1):
        log(f"{'run' if n == 0 else f'repair {n}/{strategy.rounds}'}: opencode")
        run = oc.run(prompt, workspace, session)
        session = run.stats.session_id or session
        tampered = guard.restore() if guard else []
        if tampered:
            log(f"restored protected files: {', '.join(tampered)}")
        cr = run_check(check, workspace, check_timeout) if check else None
        result.rounds.append(Round(run.exit_code, run.seconds, run.stats, cr, tampered))
        if run.infra_failure:
            log(f"opencode failed before the model ran: {run.stats.errors[0]}")
            return result
        if cr is None:
            result.passed = run.ok
            if not run.ok:
                log(f"opencode exited {run.exit_code}: {(run.stats.errors or [run.stderr.strip()[-300:]])[0]}")
            return result
        log(f"check {'passed' if cr.passed else f'failed (exit {cr.exit_code})'} in {cr.seconds:.1f}s")
        if cr.passed:
            result.passed = True
            return result
        if n < strategy.rounds:
            prompt = repair_prompt(task, check, cr, n + 1, strategy.rounds, tampered, new_session=session is None)
    return result


def solve(task: str, workdir: str | Path, check: str | None = None, oc: OpenCode | None = None,
          strategy: Strategy = PRESETS["openagent"], protect: list[str] = (), check_timeout: int = 600,
          log: Log = lambda _: None) -> Outcome:
    """Solve `task` in `workdir`. With several attempts, each runs in its own copy and the winner's
    files are synced back into `workdir`."""
    oc = oc or OpenCode()
    workdir = Path(workdir).resolve()
    if strategy.attempts <= 1:
        a = attempt(task, workdir, check, oc, strategy, protect, check_timeout, log)
        return Outcome(strategy, a.passed, 0 if a.passed else None, [a])

    with tempfile.TemporaryDirectory(prefix="openagent-") as tmp:
        copies = []
        for i in range(strategy.attempts):
            dst = Path(tmp) / f"attempt-{i}"
            shutil.copytree(workdir, dst, symlinks=True)
            copies.append(dst)

        def one(i: int) -> Attempt:
            return attempt(task, copies[i], check, oc, strategy, protect, check_timeout,
                           lambda m, i=i: log(f"[{i + 1}/{strategy.attempts}] {m}"))

        with ThreadPoolExecutor(max_workers=strategy.attempts) as pool:
            attempts = list(pool.map(one, range(strategy.attempts)))
        winner = pick_winner(attempts)
        if winner is not None:
            sync_tree(copies[winner], workdir)
            log(f"kept attempt {winner + 1}")
        for a in attempts:
            a.workspace = None  # the copies are deleted with tmp
        return Outcome(strategy, winner is not None, winner, attempts)


def pick_winner(attempts: list[Attempt]) -> int | None:
    passing = [i for i, a in enumerate(attempts) if a.passed]
    if not passing:
        return None
    return min(passing, key=lambda i: (len(attempts[i].rounds), attempts[i].stats.cost, i))


def sync_tree(src: Path, dst: Path, skip: tuple[str, ...] = (".git",)) -> None:
    """Make `dst` match `src` (except top-level `skip` entries): copy changed files, remove deleted ones."""
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if rel.parts[0] in skip:
            continue
        target = dst / rel
        if path.is_symlink():
            if target.is_symlink() or target.exists():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            target.symlink_to(path.readlink())
        elif path.is_dir():
            if target.is_symlink() or target.is_file():
                target.unlink()
            target.mkdir(exist_ok=True)
        elif not target.is_file() or target.is_symlink() or target.read_bytes() != path.read_bytes():
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            shutil.copy2(path, target)
    for path in sorted(dst.rglob("*"), reverse=True):
        rel = path.relative_to(dst)
        if rel.parts[0] in skip or (src / rel).exists() or (src / rel).is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_symlink() or path.exists():
            path.unlink()


def with_overrides(strategy: Strategy, **kw) -> Strategy:
    kw = {k: v for k, v in kw.items() if v is not None}
    return replace(strategy, **kw) if kw else strategy
