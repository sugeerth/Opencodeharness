# Open Code Harness

A small test harness for [OpenCode](https://github.com/sst/opencode). It runs `opencode run` non-interactively
on each task in a scratch copy of the task's files, then runs the task's `check.sh` to pass or fail it.
No Python dependencies; MIT licensed.

```
npm i -g opencode-ai          # or see the OpenCode install docs; configure a provider with `opencode auth login`
pip install -e '.[dev]'
ocharness examples/tasks --model anthropic/claude-sonnet-5-5 --json results.json
pytest
```

A task is a directory: `prompt.txt`, optional `files/` (starting workspace), optional `check.sh` (run in the
workspace afterwards; exit 0 = pass). See `examples/tasks/hello`.

The harness passes `--auto`, which auto-approves permissions that aren't explicitly denied, so run it in a
container or VM for untrusted prompts. Options: `--model provider/model`, `--agent`, `--timeout`, `--binary`.
