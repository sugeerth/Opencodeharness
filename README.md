# Open Code Harness

A minimal, hackable, MIT-licensed coding-agent harness. About 150 lines: a model loop, five sandboxed tools
(`read_file`, `write_file`, `edit_file`, `list_dir`, `bash`), and a CLI with approval prompts.

```
pip install -e '.[dev]'
export ANTHROPIC_API_KEY=...
ocharness "add a --verbose flag to main.py" --dir ./myproject   # asks before edits/commands
ocharness "fix the failing test" --yes                          # auto-approve
pytest
```

File tools are confined to the workspace root. `bash` is **not** sandboxed, so run it in a container for untrusted tasks.

Layout: `tools.py` (tools + schemas), `agent.py` (loop), `cli.py` (entry point). Contributions welcome.
