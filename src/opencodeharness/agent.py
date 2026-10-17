"""The agent loop: call the model, run requested tools, feed results back."""
from typing import Callable

from .tools import ToolError, Workspace

SYSTEM = "You are a coding agent. Use the tools to complete the task in the workspace, then reply with a short summary."
DANGEROUS = {"write_file", "edit_file", "bash"}


def run(task: str, workspace: Workspace, client, model: str = "claude-sonnet-5-5",
        max_turns: int = 25, approve: Callable[[str, dict], bool] | None = None,
        on_text: Callable[[str], None] = print) -> str:
    """Run the loop until the model stops calling tools. `approve` gates risky tools."""
    messages = [{"role": "user", "content": task}]
    final = ""
    for _ in range(max_turns):
        resp = client.messages.create(model=model, max_tokens=4096, system=SYSTEM,
                                      tools=workspace.schemas(), messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "text":
                final = block.text
                on_text(block.text)
            elif block.type == "tool_use":
                results.append(_execute(block, workspace, approve))
        if not results:
            return final
        messages.append({"role": "user", "content": results})
    return final + "\n[stopped: max turns reached]"


def _execute(block, workspace, approve):
    if block.name in DANGEROUS and approve and not approve(block.name, block.input):
        out, err = "denied by user", True
    else:
        try:
            out, err = workspace.call(block.name, block.input), False
        except (ToolError, OSError, TypeError) as e:
            out, err = f"error: {e}", True
    return {"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": err}
