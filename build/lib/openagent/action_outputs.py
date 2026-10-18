"""The GitHub Action's step outputs from a run record: `python3 action_outputs.py result.json >> $GITHUB_OUTPUT`.

Standalone (no imports from the package), so the action can call it by path.
"""
import json
import sys


def outputs(path: str) -> dict[str, str]:
    try:
        with open(path) as f:
            r = json.load(f)
    except (OSError, ValueError):
        r = {}
    return {"passed": "true" if r.get("passed") else "false",
            "stop": r.get("stop") or "error",
            "changed": str(sum(len(v) for v in (r.get("changes") or {}).values())),
            "cost": f"{(r.get('stats') or {}).get('cost', 0):.4f}"}


if __name__ == "__main__":
    for k, v in outputs(sys.argv[1]).items():
        print(f"{k}={v}")
