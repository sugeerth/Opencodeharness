# Open Agent

**Make [OpenCode](https://github.com/sst/opencode) verify and repair its own work.**

OpenCode is a strong open-source coding agent, but `opencode run` is one shot: it stops when the
model *thinks* it's done. Open Agent wraps it in the loop a careful engineer would run:

1. **Guide**: send the task with autonomy rules and the exact command that must pass.
2. **Verify**: run your check (`pytest`, `npm test`, `cargo test`, any shell command).
3. **Repair**: if it fails, continue the *same* OpenCode session with the failure output and go again.
4. **Guard**: files you protect (e.g. `tests/**`) are restored before every check, so the agent can't pass by editing the tests.
5. **Best-of-N** (optional): run several attempts in parallel in isolated copies and keep the cheapest one that passes.
6. **Measure**: benchmark any strategy against raw OpenCode on your own task suite, with pass rate, cost, tokens and time.

The core is Python with no dependencies, it is MIT licensed, and it works with any model OpenCode supports.

```
pip install git+https://github.com/sugeerth/Opencodeharness
npm i -g opencode-ai      # OpenCode itself; then `opencode auth login` for your provider
```

## Use it on your repo

```bash
openagent run "fix the failing date parsing tests" --check "pytest -q" --protect "tests/**"
openagent run "add a --json flag to the export command" -c "npm test" -m anthropic/claude-sonnet-5-5
openagent run "make the build pass" -c "cargo build" --attempts 3     # best-of-3
```

The exit code is 0 only if the check passes. Progress goes to stderr, and the agent's final message goes to stdout.
`--json run.json` saves the full record, including every round, tool counts, tokens and cost.

| Flag | Meaning |
|---|---|
| `-c, --check CMD` | Shell command that must exit 0. Without it OpenCode runs once, unverified. |
| `--protect GLOB` | Files the agent may not change; restored before each check (repeatable). |
| `--rounds N` | Repair rounds after the first try (default 3). |
| `--attempts N` | Parallel attempts; winner = fewest rounds, then lowest cost. |
| `--preset` | `baseline`, `guided`, `openagent` (default), `best-of-3`. Run `openagent presets`. |
| `-m, --model`, `--agent` | Passed to OpenCode (`provider/model`). |
| `--oc-arg ARG` | Any extra `opencode run` flag, e.g. `--oc-arg=--attach --oc-arg=http://localhost:4096` to skip cold starts. |
| `--no-preamble`, `--hide-check` | Turn off the guidance or the check hint. |

## Benchmark: does it actually help?

```bash
openagent bench examples/tasks --preset baseline --preset openagent --preset best-of-3 \
  -m anthropic/claude-sonnet-5-5 --repeat 3 -j 4 -o results/
```

This writes `results/report.md`, with a pass-rate table, the change against baseline in points, first-try passes, average repairs, cost,
tokens and time, plus a per-task grid. It also writes `results/results.json` with every round of every run.

A task is a directory:

```
my-task/
  prompt.txt        what to ask for
  files/            starting repository (copied fresh for every run)
  task.json         {"check": "pytest -q", "protect": ["tests/**"], "setup": "pip install -e .", "timeout": 900}
  check.sh          alternative to "check": run in the workspace; exit 0 = pass
```

`examples/tasks` includes `hello`, `fix-average` (bug fix), `slugify` (spec → implementation) and `lru-cache` (O(1) data structure).

## How it works

```
task ──► [guidance + check hint + task] ──► opencode run --format json --auto
                                                   │  events → tokens, cost, tools, session id
                                                   ▼
                               restore protected files ──► run check ──► pass ─► done
                                                                   │
                                                                 fail
                                                                   ▼
                          opencode run --session <id> "check failed, here is the output… fix the root cause"
                                                   (up to --rounds times)
```

- OpenCode runs with the workspace as its working directory, which works on both old and new CLI versions.
- If OpenCode fails before the model does anything (bad API key, network, unknown model), the loop stops and
  reports the error instead of spending repair rounds on it.
- Timeouts kill the whole process group, so no stray OpenCode servers are left running.
- Best-of-N copies the repo (including `.git`) once per attempt, then syncs the winner back. If no attempt passes, your repository is left unchanged.

Library use:

```python
from openagent import OpenCode, Strategy, solve

out = solve("fix the bug in parser.py", "path/to/repo", check="pytest -q",
            oc=OpenCode(model="anthropic/claude-sonnet-5-5"), strategy=Strategy(rounds=3, attempts=2),
            protect=["tests/**"])
print(out.passed, out.repairs, out.stats.cost)
```

## Safety

Open Agent passes `--auto`, which tells OpenCode to approve any permission that isn't explicitly denied. That
lets it run unattended, but it also means the agent can run shell commands. Use a container or VM for untrusted
tasks, or `--no-auto` together with OpenCode permission rules.

## Development

```
pip install -e '.[dev]' && pytest
```

The tests use a fake `opencode` executable (`tests/fake_opencode.py`) that emits the same JSON events as the real CLI, so they run offline in a few seconds.

MIT © Sugeerth Murugesan
