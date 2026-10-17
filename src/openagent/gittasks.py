"""Tasks from a repository's own history: `openagent tasks from-git`.

A commit that changed code and its tests is a task with a grader nobody had to write. The agent
starts from the commit's parent and is given the commit's message. The tests the commit added or
changed grade the run:

  held out (default)  they live in the task's grade/ directory, outside the workspace, and are
                      copied over the agent's work only when it has finished. The visible check
                      is the project's existing test command, so the repair loop guards against
                      regressions while the hidden tests decide the outcome.
  --reveal-tests      they are put in the workspace, are the check the agent is repaired against,
                      and are protected from edits.

A commit becomes a task only if its grader is sound for it: the commit's tests fail on the parent
(there is something to do) and pass on the commit (it can be done, and the grader agrees with the
change the project made). Every commit looked at is reported with why it was taken or why not.

Only `git` and the project's own test command are used, locally. Same idea as AgentDiff's
`--from-git` (https://github.com/sugeerth/Agenttracing), written out as Open Agent task directories.
"""
import io
import json
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Callable

PROMPT = ("In this repository, make the following change:\n\n{message}\n\n"
          "Tests that check this change exist; {tests_note}")
HELD_OUT_NOTE = "they are not given to you."
REVEALED_NOTE = "they are already in the repository: {tests}."

TRAILER = re.compile(r"^(?:co-authored-by|signed-off-by|reviewed-by|change-id|claude-session):.*$", re.I | re.M)
TEST_FILE = re.compile(r"(^|/)(tests?|spec|specs|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|"
                       r"\.(test|spec)\.[jt]sx?$|(^|/)[A-Za-z0-9]+Test\.(java|kt)$")
NOT_CODE = re.compile(r"\.(md|rst|txt|adoc|png|jpe?g|gif|svg|lock)$|(^|/)(CHANGELOG|CHANGES|HISTORY|AUTHORS|LICENSE)",
                      re.I)

Log = Callable[[str], None]


def git(repo: Path, *args: str, binary: bool = False):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return done.stdout if binary else done.stdout.decode("utf-8", "replace")


def is_test(path: str) -> bool:
    return bool(TEST_FILE.search(path))


def candidates(repo: Path, look: int = 200, most_files: int = 15, branch: str = "HEAD") -> list[dict]:
    """Commits, newest first, with one parent, that added or changed at least one test file and changed at
    least one other source file, touching no more than `most_files` files."""
    out = []
    for sha in git(repo, "log", "--no-merges", f"-n{look}", "--format=%H", branch).split():
        parents = git(repo, "log", "-1", "--format=%P", sha).split()
        if len(parents) != 1:
            continue
        changed = []
        for line in git(repo, "show", "--name-status", "--format=", sha).splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                changed.append((parts[0][0], parts[-1]))
        tests = [p for st, p in changed if is_test(p) and st in ("A", "M")]
        code = [p for st, p in changed if not is_test(p) and not NOT_CODE.search(p)]
        if tests and code and len(changed) <= most_files:
            message = TRAILER.sub("", git(repo, "log", "-1", "--format=%B", sha)).strip()
            out.append({"sha": sha, "parent": parents[0], "tests": tests, "code": code, "message": message})
    return out


def export(repo: Path, rev: str, dest: Path) -> None:
    """The tree at `rev`, without .git, into `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    data = git(repo, "archive", "--format=tar", rev, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python without extraction filters; git archive writes no absolute or .. paths
            tar.extractall(dest)


def test_command(template: str, tests: list[str]) -> str:
    files = " ".join(shlex.quote(t) for t in tests)
    return template.replace("{tests}", files) if "{tests}" in template else f"{template} {files}"


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _run(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ANSI.sub("", p.stdout + p.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "change"


def build(repo: str | Path, out_dir: str | Path, test_cmd: str = "python3 -m pytest -q", max_tasks: int = 10,
          look: int = 200, reveal: bool = False, setup: str | None = None, timeout: int = 600,
          branch: str = "HEAD", log: Log = lambda _: None) -> dict:
    """Write up to `max_tasks` validated tasks into `out_dir`; return the report of every commit looked at."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openagent-git-") as tmp:
        repo_path = Path(repo)
        if not repo_path.is_dir():  # a URL: clone it
            log(f"cloning {repo}")
            repo_path = Path(tmp) / "repo"
            subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(repo), str(repo_path)], check=True)
        report = {"repo": str(repo), "test_cmd": test_cmd, "reveal_tests": reveal, "setup": setup,
                  "taken": [], "skipped": []}
        for c in candidates(repo_path, look, branch=branch):
            if len(report["taken"]) >= max_tasks:
                break
            subject = c["message"].splitlines()[0] if c["message"] else c["sha"][:8]
            name = f"{c['sha'][:8]}-{slug(subject)}"
            grade_cmd = test_command(test_cmd, c["tests"])
            verdict = validate(repo_path, c, grade_cmd, setup, timeout, Path(tmp))
            if verdict:
                report["skipped"].append({"sha": c["sha"], "subject": subject, "why": verdict})
                log(f"skip {c['sha'][:8]} {subject[:60]}: {verdict}")
                continue
            write_task(repo_path, c, out / name, test_cmd, grade_cmd, reveal, setup)
            report["taken"].append({"sha": c["sha"], "task": name, "subject": subject, "tests": c["tests"]})
            log(f"task {name}")
    (out / "from-git.json").write_text(json.dumps(report, indent=2))
    return report


def validate(repo: Path, c: dict, grade_cmd: str, setup: str | None, timeout: int, tmp: Path) -> str:
    """Empty if the commit's tests fail on the parent and pass on the commit; else why not."""
    for rev, want_pass in ((c["parent"], False), (c["sha"], True)):
        d = tmp / f"check-{rev[:12]}"
        if d.exists():
            shutil.rmtree(d)
        export(repo, rev, d)
        for t in c["tests"]:  # the commit's tests, on either tree
            dst = d / t
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(git(repo, "show", f"{c['sha']}:{t}", binary=True))
        if setup:
            code, outp = _run(setup, d, timeout)
            if code != 0:
                shutil.rmtree(d, ignore_errors=True)
                return f"setup failed on {'the commit' if want_pass else 'the parent'}: {outp.strip()[-200:]}"
        code, outp = _run(grade_cmd, d, timeout)
        shutil.rmtree(d, ignore_errors=True)
        if want_pass and code != 0:
            return f"its tests fail on the commit itself (exit {code}): {outp.strip()[-200:]}"
        if not want_pass and code == 0:
            return "its tests already pass on the parent: nothing to do"
        if not want_pass and code in (126, 127):
            return f"the test command could not run (exit {code})"
    return ""


def write_task(repo: Path, c: dict, d: Path, test_cmd: str, grade_cmd: str, reveal: bool, setup: str | None) -> None:
    if d.exists():
        shutil.rmtree(d)
    export(repo, c["parent"], d / "files")
    hidden = d / ("files" if reveal else "grade")
    for t in c["tests"]:
        dst = hidden / t
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(git(repo, "show", f"{c['sha']}:{t}", binary=True))
    note = REVEALED_NOTE.format(tests=", ".join(c["tests"])) if reveal else HELD_OUT_NOTE
    (d / "prompt.txt").write_text(PROMPT.format(message=c["message"], tests_note=note) + "\n")
    cfg: dict = {"source": {"sha": c["sha"], "parent": c["parent"], "tests": c["tests"]}}
    if reveal:
        cfg.update(check=grade_cmd, protect=list(c["tests"]))
    else:
        cfg.update(check=test_cmd.replace("{tests}", "").strip(), grade=f"cp -r {{task}}/grade/. . && {grade_cmd}")
    if setup:
        cfg["setup"] = setup
    (d / "task.json").write_text(json.dumps(cfg, indent=2) + "\n")
