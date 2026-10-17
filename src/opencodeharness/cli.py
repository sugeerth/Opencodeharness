import argparse
import json


def main(argv=None):
    import anthropic
    from .agent import run
    from .tools import Workspace

    p = argparse.ArgumentParser(prog="ocharness", description="Minimal coding-agent harness")
    p.add_argument("task")
    p.add_argument("--dir", default=".", help="workspace root")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--yes", action="store_true", help="auto-approve file edits and shell commands")
    a = p.parse_args(argv)

    def approve(name, args):
        return a.yes or input(f"Allow {name} {json.dumps(args)[:200]}? [y/N] ").lower() == "y"

    run(a.task, Workspace(a.dir), anthropic.Anthropic(), a.model, approve=approve)
