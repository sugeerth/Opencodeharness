from types import SimpleNamespace as NS

import pytest

from opencodeharness.agent import run
from opencodeharness.tools import ToolError, Workspace


def test_workspace_blocks_escape(tmp_path):
    with pytest.raises(ToolError):
        Workspace(tmp_path).read_file("../x")


def test_edit_requires_unique_match(tmp_path):
    ws = Workspace(tmp_path)
    ws.write_file("a.txt", "x x")
    with pytest.raises(ToolError):
        ws.edit_file("a.txt", "x", "y")
    ws.write_file("a.txt", "x z")
    ws.edit_file("a.txt", "x", "y")
    assert ws.read_file("a.txt") == "y z"


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = NS(create=lambda **kw: next(self.replies))


def test_loop_runs_tool_then_finishes(tmp_path):
    use = NS(type="tool_use", id="1", name="write_file", input={"path": "h.txt", "content": "hi"})
    done = NS(type="text", text="done")
    client = FakeClient([NS(content=[use]), NS(content=[done])])
    out = run("make file", Workspace(tmp_path), client, on_text=lambda t: None)
    assert out == "done" and (tmp_path / "h.txt").read_text() == "hi"


def test_denied_tool_not_executed(tmp_path):
    use = NS(type="tool_use", id="1", name="bash", input={"command": "touch z"})
    client = FakeClient([NS(content=[use]), NS(content=[NS(type="text", text="ok")])])
    run("t", Workspace(tmp_path), client, approve=lambda n, a: False, on_text=lambda t: None)
    assert not (tmp_path / "z").exists()
