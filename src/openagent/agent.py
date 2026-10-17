"""Open Agent: wrap OpenCode in a verify -> repair loop, with optional best-of-N attempts.

One attempt:
  1. Send the task to OpenCode (with autonomy guidance and the check command, unless disabled).
  2. Restore any protected files the agent changed, then run the check.
  3. If it fails, continue the same OpenCode session with the failing part of the output and
     repeat, up to `rounds` repairs. A round that changes no files gets a sharper prompt; two in a
     row end the attempt as stalled. A cost budget, if set, ends it too.

Before any of that the check runs once on the untouched workspace, so a check that cannot run
at all (command not found) is reported instead of being handed to the agent as a code failure.

Best-of-N runs several attempts in parallel in separate copies of the workspace and keeps the
passing one with the fewest rounds, then the lowest cost.
"""
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .events import RunStats
from .opencode import OpenCode
from .verify import CheckResult, Guard, changes, excerpt, fingerprint, run_check

PREAMBLE = """You are working autonomously in this repository. No human will answer questions, so make reasonable assumptions and keep going.
- Start by reading the code relevant to the task; do not guess at structure.
- Make the smallest change that fully solves the task, matching the existing style.
- Before you finish, run the relevant tests or checks yourself and fix any failures.
- Never weaken, skip or delete tests or checks to make them pass."""

CHECK_HINT = "Your work will be verified by running `{check}` in the repository root. It must exit 0."

REPAIR = """Verification failed (repair {n} of {total}). `{check}` exited {code}. The relevant output:
```
{output}
```
{notes}Find the root cause and fix the code. Do not modify the tests or checks."""

REPAIR_NEW_SESSION = "You are continuing an earlier attempt at this task:\n\n{task}\n\n"
TAMPER = "Note: you changed protected files ({files}); they were restored before checking.\n"
NO_PROGRESS = ("Note: your last turn changed no files, so the result could not change. Re-read the "
               "failing output and the code it points at, and make an actual edit.\n")
CHANGED = "Files you have changed so far: {files}.\n"

# Exit codes a shell uses when the command itself could not be run.
NOT_RUNNABLE = {126, 127}


@dataclass(frozen=True)
class Strategy:
    name: str = "openagent"
    preamble: bool = True        # prepend autonomy guidance
    reveal_check: bool = True    # tell the agent the check command
    rounds: int = 3              # repair rounds after the first try
    attempts: int = 1            # independent parallel attempts (best-of-N)
    instructions: tuple[str, ...] = ()   # extra rules, e.g. ones `openagent evolve` kept

    def to_dict(self) -> dict:
        return {"name": self.name, "preamble": self.preamble, "reveal_check": self.reveal_check,
                "rounds": self.rounds, "attempts": self.attempts, "instructions": list(self.instructions)}

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        known = {k: d[k] for k in ("name", "preamble", "reveal_check", "rounds", "attempts") if k in d}
        return cls(**known, instructions=tuple(d.get("instructions", ())))

    @classmethod
    def load(cls, path: str | Path) -> "Strategy":
        """A harness file: the JSON `openagent evolve` writes (harness.json), or one written by hand."""
        import json
        return cls.from_dict(json.loads(Path(path).read_text()))


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
    prompt: str = ""
    events: list[dict] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)   # files this round changed
    stderr: str = ""
    retried: bool = False                               # OpenCode printed nothing and was run again

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "seconds": round(self.seconds, 2), "stats": self.stats.to_dict(),
                "check": None if self.check is None else {"passed": self.check.passed, "exit_code": self.check.exit_code,
                                                          "seconds": round(self.check.seconds, 2)},
                "tampered": self.tampered, "changed": self.changed, "stderr": self.stderr[-1500:],
                "retried": self.retried}


@dataclass
class Attempt:
    passed: bool
    rounds: list[Round] = field(default_factory=list)
    workspace: Path | None = None
    stop: str = ""                                          # why the attempt ended
    changes: dict = field(default_factory=lambda: {"added": [], "modified": [], "deleted": []})

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
        return {"passed": self.passed, "stop": self.stop, "seconds": round(self.seconds, 2),
                "stats": self.stats.to_dict(), "changes": self.changes,
                "rounds": [r.to_dict() for r in self.rounds]}


@dataclass
class Outcome:
    strategy: Strategy
    passed: bool
    winner: int | None
    attempts: list[Attempt]
    preflight: CheckResult | None = None
    error: str = ""

    @property
    def best(self) -> Attempt | None:
        if self.winner is not None:
            return self.attempts[self.winner]
        return self.attempts[0] if self.attempts else None

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
        return max(len(self.best.rounds) - 1, 0) if self.best else 0

    def to_dict(self) -> dict:
        b = self.best
        return {"strategy": self.strategy.name, "passed": self.passed, "winner": self.winner,
                "repairs": self.repairs, "stop": b.stop if b else "error", "error": self.error,
                "already_passing": bool(self.preflight and self.preflight.passed),
                "seconds": round(self.seconds, 2), "stats": self.stats.to_dict(),
                "changes": b.changes if b else {}, "attempts": [a.to_dict() for a in self.attempts]}


Log = Callable[[str], None]


def first_prompt(task: str, check: str | None, strategy: Strategy) -> str:
    parts = []
    rules = "".join(f"\n- {i}" for i in strategy.instructions)
    if strategy.preamble:
        parts.append(PREAMBLE + rules)
    elif rules:
        parts.append("Rules:" + rules)
    if check and strategy.reveal_check:
        parts.append(CHECK_HINT.format(check=check))
    parts.append(("Task:\n" if parts else "") + task)
    return "\n\n".join(parts)


def repair_prompt(task: str, check: str, result: CheckResult, n: int, total: int, tampered: list[str],
                  new_session: bool, no_progress: bool = False, changed_so_far: list[str] = ()) -> str:
    notes = ""
    if tampered:
        notes += TAMPER.format(files=", ".join(tampered))
    if no_progress:
        notes += NO_PROGRESS
    if changed_so_far:
        shown = list(changed_so_far)[:20]
        notes += CHANGED.format(files=", ".join(shown) + (" ..." if len(changed_so_far) > 20 else ""))
    msg = REPAIR.format(n=n, total=total, check=check, code=result.exit_code,
                        output=excerpt(result.output), notes=notes)
    return (REPAIR_NEW_SESSION.format(task=task) if new_session else "") + msg


def attempt(task: str, workspace: Path, check: str | None, oc: OpenCode, strategy: Strategy,
            protect: list[str] = (), check_timeout: int = 600, log: Log = lambda _: None,
            max_cost: float | None = None) -> Attempt:
    guard = Guard(workspace, list(protect)) if protect else None
    result = Attempt(passed=False, workspace=workspace)
    start = prev = fingerprint(workspace)
    prompt, session, idle = first_prompt(task, check, strategy), None, 0

    def finish(stop: str) -> Attempt:
        result.stop = stop
        result.changes = changes(start, fingerprint(workspace))
        return result

    for n in range(strategy.rounds + 1):
        log(f"{'run' if n == 0 else f'repair {n}/{strategy.rounds}'}: opencode")
        run = oc.run(prompt, workspace, session)
        retried = False
        if run.empty and run.exit_code != 127:
            log(f"opencode produced no output (exit {run.exit_code}); retrying once"
                + (f": {run.stderr.strip()[-200:]}" if run.stderr.strip() else ""))
            run, retried = oc.run(prompt, workspace, session), True
            if run.empty and not run.stats.errors:
                run.stats.errors.append("OpenCode produced no output twice"
                                        + (f": {run.stderr.strip()[-300:]}" if run.stderr.strip() else ""))
        session = run.stats.session_id or session
        tampered = guard.restore() if guard else []
        if tampered:
            log(f"restored protected files: {', '.join(tampered)}")
        now = fingerprint(workspace)
        changed = [p for group in changes(prev, now).values() for p in group]
        prev = now
        cr = run_check(check, workspace, check_timeout) if check else None
        result.rounds.append(Round(run.exit_code, run.seconds, run.stats, cr, tampered, prompt, run.events, changed,
                                   run.stderr, retried))
        if run.infra_failure:
            log(f"opencode failed before the model ran: {run.stats.errors[0]}")
            return finish("infra_error")
        if cr is None:
            result.passed = run.ok
            if not run.ok:
                log(f"opencode exited {run.exit_code}: {(run.stats.errors or [run.stderr.strip()[-300:]])[0]}")
            return finish("done" if run.ok else "agent_error")
        log(f"check {'passed' if cr.passed else f'failed (exit {cr.exit_code})'} in {cr.seconds:.1f}s"
            + ("" if changed else ", no files changed"))
        if cr.passed:
            result.passed = True
            return finish("passed")
        idle = 0 if changed else idle + 1
        if n > 0 and idle >= 2:
            log("two rounds in a row changed nothing; stopping")
            return finish("stalled")
        spent = result.stats.cost
        if max_cost is not None and spent >= max_cost:
            log(f"cost budget reached (${spent:.4f} >= ${max_cost:.4f})")
            return finish("budget")
        if n < strategy.rounds:
            so_far = [p for group in changes(start, now).values() for p in group]
            prompt = repair_prompt(task, check, cr, n + 1, strategy.rounds, tampered,
                                   new_session=session is None, no_progress=not changed, changed_so_far=so_far)
    return finish("rounds_exhausted" if strategy.rounds else "failed")


def preflight(check: str, workdir: Path, timeout: int) -> tuple[CheckResult, str]:
    """Run the check on the untouched workspace. Returns (result, error) where error is set
    if the check could not run at all."""
    cr = run_check(check, workdir, timeout)
    if cr.exit_code in NOT_RUNNABLE or "command not found" in cr.output.lower():
        return cr, f"the check could not run (exit {cr.exit_code}): {cr.output.strip()[-300:]}"
    return cr, ""


def solve(task: str, workdir: str | Path, check: str | None = None, oc: OpenCode | None = None,
          strategy: Strategy = PRESETS["openagent"], protect: list[str] = (), check_timeout: int = 600,
          log: Log = lambda _: None, max_cost: float | None = None, preflight_check: bool = True) -> Outcome:
    """Solve `task` in `workdir`. With several attempts, each runs in its own copy and the winner's
    files are synced back into `workdir`."""
    oc = oc or OpenCode()
    workdir = Path(workdir).resolve()
    pre = None
    if check and preflight_check:
        pre, err = preflight(check, workdir, check_timeout)
        if err:
            log(err)
            return Outcome(strategy, False, None, [], pre, err)
        log("check already passes before any change" if pre.passed else
            f"check fails before any change (exit {pre.exit_code}), as expected")

    budget = max_cost / strategy.attempts if max_cost is not None and strategy.attempts > 1 else max_cost
    if strategy.attempts <= 1:
        a = attempt(task, workdir, check, oc, strategy, protect, check_timeout, log, budget)
        return Outcome(strategy, a.passed, 0 if a.passed else None, [a], pre)

    with tempfile.TemporaryDirectory(prefix="openagent-") as tmp:
        copies = []
        for i in range(strategy.attempts):
            dst = Path(tmp) / f"attempt-{i}"
            shutil.copytree(workdir, dst, symlinks=True)
            copies.append(dst)

        def one(i: int) -> Attempt:
            return attempt(task, copies[i], check, oc, strategy, protect, check_timeout,
                           lambda m, i=i: log(f"[{i + 1}/{strategy.attempts}] {m}"), budget)

        with ThreadPoolExecutor(max_workers=strategy.attempts) as pool:
            attempts = list(pool.map(one, range(strategy.attempts)))
        winner = pick_winner(attempts)
        if winner is not None:
            sync_tree(copies[winner], workdir)
            log(f"kept attempt {winner + 1}")
        for a in attempts:
            a.workspace = None  # the copies are deleted with tmp
        return Outcome(strategy, winner is not None, winner, attempts, pre)


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
