# Open Agent

**An open-source agent on top of [OpenCode](https://github.com/sst/opencode) that verifies and repairs its own work, with every run evaluated by [AgentDiff](https://github.com/sugeerth/Agenttracing).**

OpenCode is a strong open-source coding agent, but `opencode run` is one shot: it stops when the
model *thinks* it's done. Open Agent runs the loop a careful engineer would:

1. **Preflight**: run your check on the untouched repo, and stop with a clear error if it can't run at all.
2. **Guide**: send the task with autonomy rules and the exact command that must pass.
3. **Verify**: run the check (`pytest`, `npm test`, `cargo test`, any shell command).
4. **Repair**: if it fails, continue the *same* OpenCode session with the failing lines and go again.
5. **Guard**: files you protect (e.g. `tests/**`) are restored before every check, so weakening a test never passes.
6. **Know when to stop**: two rounds that change nothing end as `stalled`; a provider or network failure ends at once; `--max-cost` caps spend.
7. **Best-of-N**: run attempts in parallel in isolated copies and keep the passing one with the fewest rounds.
8. **Measure**: benchmark strategies against raw OpenCode, graded by held-out tests the agent never sees, and write every run as an AgentDiff trace for paired inference, pass^k, diagnosis and a scorecard.

The core is Python 3.11+ with no dependencies, it is MIT licensed, and it works with any model OpenCode supports.

**[See the whole workflow, end to end →](docs/WORKFLOW.md)**

```bash
pip install git+https://github.com/sugeerth/Opencodeharness
npm i -g opencode-ai           # OpenCode itself; then `opencode auth login` for your provider
```

## Use it on your repo

```bash
openagent run "fix the failing date parsing tests" --check "pytest -q" --protect "tests/**"
openagent run "add a --json flag to the export command" -c "npm test" -m anthropic/claude-sonnet-5-5
openagent run "make the build pass" -c "cargo build" --attempts 3 --max-cost 2.00
```

The exit code is 0 only if the check passes. Progress goes to stderr, and the agent's final message goes to stdout.
`--json run.json` saves the full record and `--trace traces/` writes an AgentDiff trace.

| Flag | Meaning |
|---|---|
| `-c, --check CMD` | Shell command that must exit 0. Without it OpenCode runs once, unverified. |
| `--protect GLOB` | Files the agent may not change; restored before each check (repeatable). |
| `--rounds N` | Repair rounds after the first try (default 3). |
| `--attempts N` | Parallel attempts; winner = fewest rounds, then lowest cost. |
| `--max-cost USD` | Stop once the runs have cost this much. |
| `--preset` | `baseline`, `guided`, `openagent` (default), `best-of-3`. Run `openagent presets`. |
| `-m, --model`, `--agent` | Passed to OpenCode (`provider/model`). |
| `--oc-arg ARG` | Any extra `opencode run` flag, e.g. `--oc-arg=--attach --oc-arg=http://localhost:4096` to skip cold starts. |
| `--no-preamble`, `--hide-check` | Turn off the guidance or the check hint. |

## See the whole workflow, offline: `openagent demo`

```bash
git clone https://github.com/sugeerth/Opencodeharness && cd Opencodeharness
pip install -e . git+https://github.com/sugeerth/Agenttracing
openagent demo --repeat 3 --agentdiff
```

This runs the **real OpenCode**, with its tools, sessions and event stream, against a scripted model
served on localhost (`openagent.sim`), so it needs no API key. Six example tasks each exercise one part of the loop:

| Task | What it shows |
|---|---|
| `hello` | every strategy passes |
| `fix-average` | the agent's own test run isn't enough; a repair round from Open Agent is |
| `slugify` | the guidance alone fixes it: the agent runs the tests and fixes the accents |
| `parse-duration` | the agent weakens a test; the guard restores it; then it writes a real fix |
| `fix-config` | the agent stops making edits; Open Agent stops it as `stalled` |
| `lru-cache` | passes the visible tests, fails a held-out performance test: repair can't fix what the check doesn't test |

The playbooks decide those outcomes, so the demo shows the workflow, not how good any model is. The
committed run is in [`docs/demo`](docs/demo/), walked through in [docs/WORKFLOW.md](docs/WORKFLOW.md).

## Benchmark with a real model

```bash
openagent bench examples/tasks --preset baseline --preset openagent --preset best-of-3 \
  -m anthropic/claude-sonnet-5-5 --repeat 5 -j 4 --agentdiff -o results/
```

This writes `results/report.md`, which has:
- pass rate graded by held-out tests, beside the visible check
- the change against baseline in points, first-try passes, repairs, tokens, cost and time
- how each run ended
- a task-by-task comparison with AgentDiff's keep rule: keep a change only if it wins more tasks than it loses and loses none

Alongside it go `results/results.json`, one AgentDiff trace per run in `results/traces/<strategy>/`, and with
`--agentdiff`, `agentdiff runs` and `agentdiff eval` for each strategy against baseline.

A task is a directory:

```
my-task/
  prompt.txt        what to ask for
  files/            starting repository (copied fresh for every run)
  task.json         {"check": "pytest -q", "protect": ["tests/**"],
                     "grade": "bash {task}/hidden_tests.sh", "setup": "pip install -e .", "timeout": 900}
  check.sh          alternative to "check": run in the workspace; exit 0 = pass
```

`grade` is held out: it runs once at the end, the agent never sees it, and its verdict is the one that
counts. `{task}` is the task directory, so hidden files can live beside `files/`.

## AgentDiff traces

Every run becomes an AgentDiff trace (SCHEMA v1). It contains:
- OpenCode's tool calls, with their read/write effect and real status declared
- the model's text and reasoning
- tokens and timing from the event stream
- Open Agent's own steps: repair prompts, guard restores, checks, with check re-runs marked as the harness's attempts
- a termination reason from AgentDiff's vocabulary (`infrastructure_error` for runs that never reached the model, which AgentDiff leaves out of reliability statistics)

```bash
agentdiff runs results/agentdiff/baseline-vs-openagent/traces -o out/   # paired inference, pass^k, diagnosis
agentdiff compare results/traces/baseline/t__baseline__r1.json results/traces/openagent/t__openagent__r1.json
agentdiff eval results/agentdiff/baseline-vs-openagent/traces -o eval/  # the scorecard
```

## Library

```python
from openagent import OpenCode, Strategy, solve

out = solve("fix the bug in parser.py", "path/to/repo", check="pytest -q",
            oc=OpenCode(model="anthropic/claude-sonnet-5-5"), strategy=Strategy(rounds=3, attempts=2),
            protect=["tests/**"], max_cost=1.0)
print(out.passed, out.best.stop, out.repairs, out.best.changes, out.stats.cost)
```

## How it works

- OpenCode runs in the workspace with `$PWD` set to it; OpenCode takes its project directory from `$PWD`,
  so `cwd` alone would point it at whatever directory you launched from.
- Repair prompts carry the lines of the check's output that look like failures (with context) plus the tail,
  the files changed so far, and notes when protected files were restored or the last turn changed nothing.
- The guard ignores caches the check itself rewrites (`__pycache__`, `.pyc`), so it never accuses the agent of the test runner's writes.
- Timeouts kill the whole process group, so no stray OpenCode servers are left running.
- Best-of-N copies the repo (including `.git`) per attempt, then syncs the winner back. If no attempt passes, your repository is left unchanged.

## Safety

Open Agent passes `--auto`, which tells OpenCode to approve any permission that isn't explicitly denied. That
lets it run unattended, but it also means the agent can run shell commands. Use a container or VM for untrusted
tasks, or `--no-auto` together with OpenCode permission rules.

## Development

```bash
pip install -e '.[dev]' && pytest
```

The unit tests use a fake `opencode` (`tests/fake_opencode.py`) that emits the real JSON event format and
run offline in seconds. One test drives the real OpenCode against the scripted model end to end; it runs
wherever `opencode` is installed (CI installs it) and is skipped elsewhere.

MIT © Sugeerth Murugesan
