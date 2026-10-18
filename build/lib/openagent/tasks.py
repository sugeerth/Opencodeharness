"""Benchmark tasks on disk.

A task is a directory containing:
  prompt.txt   the instruction for the agent (required)
  files/       the starting workspace (optional)
  check.sh     run in the workspace afterwards; exit 0 = pass (optional)
  task.json    optional settings:
                 "check":   shell command the agent is told about and repaired against
                 "grade":   held-out shell command, run once at the end; the agent never sees it,
                            and when present its verdict is the one that counts
                 "protect": globs the agent may not change, e.g. ["tests/**"]
                 "setup":   shell command run in the fresh workspace before the agent starts
                 "timeout": seconds per OpenCode run
In any command, {task} is replaced by the task directory's absolute path, so held-out files can
live beside files/ where the agent can't see them.
"""
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Task:
    name: str
    prompt: str
    files: Path | None = None
    check: str | None = None
    protect: list[str] = field(default_factory=list)
    setup: str | None = None
    timeout: int | None = None
    grade: str | None = None

    @classmethod
    def load(cls, d: str | Path) -> "Task":
        d = Path(d).resolve()
        cfg = json.loads((d / "task.json").read_text()) if (d / "task.json").is_file() else {}
        here = shlex.quote(str(d))

        def cmd(key: str) -> str | None:
            v = cfg.get(key)
            return v.replace("{task}", here) if isinstance(v, str) else None

        check = cmd("check")
        if check is None and (d / "check.sh").is_file():
            check = f"bash {shlex.quote(str(d / 'check.sh'))}"
        return cls(name=d.name, prompt=(d / "prompt.txt").read_text().strip(),
                   files=d / "files" if (d / "files").is_dir() else None,
                   check=check, protect=list(cfg.get("protect", [])),
                   setup=cmd("setup"), timeout=cfg.get("timeout"), grade=cmd("grade"))


def discover(root: str | Path) -> list[Task]:
    """Load every task under `root` (or `root` itself if it is a task)."""
    root = Path(root)
    if (root / "prompt.txt").is_file():
        return [Task.load(root)]
    return [Task.load(d) for d in sorted(root.iterdir()) if (d / "prompt.txt").is_file()]


def resolve(source: str | Path, cache: str | Path, log=lambda _: None) -> Path:
    """A task directory for `source`: itself, or, for a manifest (.json), the tasks fetched from it into
    `cache` (once; later calls reuse them)."""
    source = Path(source)
    if source.suffix != ".json":
        return source
    cache = Path(cache)
    if not (cache / "fetch.json").is_file():
        from .gittasks import fetch
        log(f"fetching the tasks of {source.name} into {cache} (first use only)")
        fetch(json.loads(source.read_text()), cache, log=log)
    return cache
