"""Tools the agent can call. Each tool is a schema plus a Python function."""
import subprocess
from pathlib import Path

MAX_OUTPUT = 20_000


class ToolError(Exception):
    pass


class Workspace:
    """File and shell tools confined to a root directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, p: str) -> Path:
        full = (self.root / p).resolve()
        if full != self.root and self.root not in full.parents:
            raise ToolError(f"path escapes workspace: {p}")
        return full

    def read_file(self, path: str) -> str:
        f = self._path(path)
        if not f.is_file():
            raise ToolError(f"not a file: {path}")
        return f.read_text()[:MAX_OUTPUT]

    def write_file(self, path: str, content: str) -> str:
        f = self._path(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        return f"wrote {len(content)} bytes to {path}"

    def edit_file(self, path: str, old: str, new: str) -> str:
        f = self._path(path)
        text = f.read_text()
        if text.count(old) != 1:
            raise ToolError(f"old string must occur exactly once (found {text.count(old)})")
        f.write_text(text.replace(old, new))
        return f"edited {path}"

    def list_dir(self, path: str = ".") -> str:
        d = self._path(path)
        if not d.is_dir():
            raise ToolError(f"not a directory: {path}")
        return "\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in d.iterdir()))

    def bash(self, command: str, timeout: int = 60) -> str:
        try:
            r = subprocess.run(command, shell=True, cwd=self.root, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"timed out after {timeout}s")
        out = (r.stdout + r.stderr)[-MAX_OUTPUT:]
        return f"exit {r.returncode}\n{out}"

    def schemas(self) -> list[dict]:
        def tool(name, desc, props, required):
            return {"name": name, "description": desc,
                    "input_schema": {"type": "object", "properties": props, "required": required}}
        s = {"type": "string"}
        return [
            tool("read_file", "Read a text file.", {"path": s}, ["path"]),
            tool("write_file", "Create or overwrite a file.", {"path": s, "content": s}, ["path", "content"]),
            tool("edit_file", "Replace one exact occurrence of a string in a file.",
                 {"path": s, "old": s, "new": s}, ["path", "old", "new"]),
            tool("list_dir", "List a directory.", {"path": s}, []),
            tool("bash", "Run a shell command in the workspace.", {"command": s}, ["command"]),
        ]

    def call(self, name: str, args: dict) -> str:
        fn = getattr(self, name, None)
        if name not in {"read_file", "write_file", "edit_file", "list_dir", "bash"} or fn is None:
            raise ToolError(f"unknown tool: {name}")
        return fn(**args)
