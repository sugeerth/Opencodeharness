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


def origin_url(repo: Path) -> str:
    try:
        return git(repo, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        return ""


def candidate_for(repo: Path, sha: str) -> dict | None:
    """One commit as a candidate, by its hash: what `candidates` would have produced for it."""
    parents = git(repo, "log", "-1", "--format=%P", sha).split()
    if len(parents) != 1:
        return None
    changed = name_status(repo, sha)
    tests = [p for st, p in changed if is_test(p) and st in ("A", "M")]
    code = [p for st, p in changed if not is_test(p) and not NOT_CODE.search(p)]
    if not tests or not code:
        return None
    message = TRAILER.sub("", git(repo, "log", "-1", "--format=%B", sha)).strip()
    return {"sha": git(repo, "rev-parse", sha).strip(), "parent": parents[0], "tests": tests, "code": code,
            "message": message}


def candidates(repo: Path, look: int = 200, most_files: int = 15, branch: str = "HEAD") -> list[dict]:
    """Commits, newest first, with one parent, that added or changed at least one test file and changed at
    least one other source file, touching no more than `most_files` files."""
    out = []
    for sha in git(repo, "log", "--no-merges", f"-n{look}", "--format=%H", branch).split():
        parents = git(repo, "log", "-1", "--format=%P", sha).split()
        if len(parents) != 1:
            continue
        changed = name_status(repo, sha)
        tests = [p for st, p in changed if is_test(p) and st in ("A", "M")]
        code = [p for st, p in changed if not is_test(p) and not NOT_CODE.search(p)]
        if tests and code and len(changed) <= most_files:
            message = TRAILER.sub("", git(repo, "log", "-1", "--format=%B", sha)).strip()
            out.append({"sha": sha, "parent": parents[0], "tests": tests, "code": code, "message": message})
    return out


def name_status(repo: Path, sha: str) -> list[tuple[str, str]]:
    """[(status letter, path)] for a commit. -z keeps paths with non-ASCII characters, tabs or quotes
    as they are, instead of C-quoting them (which `git show sha:path` would not understand)."""
    fields = git(repo, "show", "-z", "--name-status", "--format=", sha).split("\0")
    out, i = [], 0
    while i < len(fields):
        st = fields[i].strip()
        if not st:
            i += 1
            continue
        if st[0] in "RC":           # renames and copies: status, old path, new path
            if i + 2 < len(fields):
                out.append((st[0], fields[i + 2]))
            i += 3
        else:
            if i + 1 < len(fields):
                out.append((st[0], fields[i + 1]))
            i += 2
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
        report = {"repo": str(repo), "url": origin_url(repo_path) or str(repo), "test_cmd": test_cmd,
                  "reveal_tests": reveal, "setup": setup, "taken": [], "skipped": []}
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
            write_task(repo_path, c, out / name, test_cmd, grade_cmd, reveal, setup, timeout)
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
        if not want_pass and code == -1:
            return f"its tests timed out on the parent ({timeout}s): a timeout is not a sound failure"
    return ""


def visible_check_works(files: Path, check: str, setup: str | None, timeout: int) -> str:
    """Empty if the visible check passes on the untouched starting tree; else why not. A check that
    already fails there (no tests left once the commit's are held out, or old tests that don't run in
    this environment) would only send the repair loop after failures the agent didn't cause."""
    with tempfile.TemporaryDirectory(prefix="openagent-visible-") as tmp:
        w = Path(tmp) / "w"
        shutil.copytree(files, w, symlinks=True)
        if setup:
            code, outp = _run(setup, w, timeout)
            if code != 0:
                return f"setup failed: {outp.strip()[-160:]}"
        code, outp = _run(check, w, timeout)
    if code == 0:
        return ""
    tail = outp.strip().splitlines()[-1] if outp.strip() else ""
    return f"the project's test command fails on the untouched starting code (exit {code}: {tail[:120]})"


def write_task(repo: Path, c: dict, d: Path, test_cmd: str, grade_cmd: str, reveal: bool, setup: str | None,
               timeout: int = 600) -> None:
    if d.exists():
        shutil.rmtree(d)
    export(repo, c["parent"], d / "files")
    if not reveal:
        # The commit's tests are the grader. A test file it modified must not stay in the workspace in
        # its old form: the visible check would then fail on the correct change and the repair loop
        # would push the agent back towards the old behaviour.
        for t in c["tests"]:
            old = d / "files" / t
            if old.is_file() or old.is_symlink():
                old.unlink()
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
        check = test_cmd.replace("{tests}", "").strip()
        why = visible_check_works(d / "files", check, setup, timeout)
        if why:  # the hidden tests alone decide; the task runs without repair against a broken check
            cfg.update(grade=f"cp -r {{task}}/grade/. . && {grade_cmd}", check_dropped=why)
        else:
            cfg.update(check=check, grade=f"cp -r {{task}}/grade/. . && {grade_cmd}")
    if setup:
        cfg["setup"] = setup
    (d / "task.json").write_text(json.dumps(cfg, indent=2) + "\n")


# ---- manifests: a benchmark as a small file, rebuilt from the repositories on demand --------------

def export_manifest(dirs: list[str | Path], name: str = "benchmark") -> dict:
    """A manifest from the from-git.json of each task directory `tasks from-git` wrote."""
    repos = []
    for d in map(Path, dirs):
        r = json.loads((d / "from-git.json").read_text())
        if not r["taken"]:
            continue
        repos.append({"url": r.get("url") or r["repo"], "test_cmd": r["test_cmd"], "setup": r.get("setup"),
                      "reveal_tests": r.get("reveal_tests", False),
                      "tasks": [{"sha": t["sha"], "task": t["task"], "subject": t["subject"], "tests": t["tests"]}
                                for t in r["taken"]]})
    return {"name": name, "format": "openagent-manifest/1",
            "tasks": sum(len(r["tasks"]) for r in repos), "repos": repos}


def fetch(manifest: dict, out_dir: str | Path, validate_tasks: bool = True, timeout: int = 600,
          only: list[str] | None = None, log: Log = lambda _: None) -> dict:
    """Rebuild a manifest's tasks: clone each repository once, then write every listed commit as a task,
    re-validating it (its tests fail on the parent and pass on the commit) unless told not to."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = {"manifest": manifest.get("name"), "written": [], "failed": []}
    with tempfile.TemporaryDirectory(prefix="openagent-fetch-") as tmp:
        for i, r in enumerate(manifest["repos"]):
            if only and not any(o in r["url"] for o in only):
                continue
            repo = Path(r["url"]) if Path(r["url"]).is_dir() else Path(tmp) / f"repo-{i}"
            if not Path(r["url"]).is_dir():
                log(f"cloning {r['url']}")
                subprocess.run(["git", "clone", "--quiet", "--no-checkout", r["url"], str(repo)], check=True)
            for t in r["tasks"]:
                c = None
                try:
                    c = candidate_for(repo, t["sha"])
                except subprocess.CalledProcessError as e:
                    why = f"commit not found: {e.stderr.decode(errors='replace').strip()[-120:]}"
                else:
                    why = "" if c else "no longer a test-and-code commit"
                if c and validate_tasks:
                    why = validate(repo, c, test_command(r["test_cmd"], c["tests"]), r.get("setup"), timeout, Path(tmp))
                if why:
                    report["failed"].append({"task": t["task"], "why": why})
                    log(f"skip {t['task']}: {why}")
                    continue
                write_task(repo, c, out / t["task"], r["test_cmd"], test_command(r["test_cmd"], c["tests"]),
                           r.get("reveal_tests", False), r.get("setup"), timeout)
                report["written"].append(t["task"])
                log(f"task {t['task']}")
    (out / "fetch.json").write_text(json.dumps(report, indent=2))
    return report
