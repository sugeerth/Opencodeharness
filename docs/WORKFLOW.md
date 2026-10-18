# The workflow, end to end

Open Agent is an agent that runs on top of [OpenCode](https://github.com/sst/opencode): OpenCode does
the work, and Open Agent decides when the work is done. Every run can be evaluated by
[AgentDiff](https://github.com/sugeerth/Agenttracing). This page walks through the whole loop on the
run committed in [`docs/demo`](demo/), which you can reproduce with one command:

```bash
git clone https://github.com/sugeerth/Opencodeharness && cd Opencodeharness
pip install -e . git+https://github.com/sugeerth/Agenttracing
npm i -g opencode-ai
openagent demo --preset baseline --preset guided --preset openagent \
  --harness docs/evolve/harness.json --repeat 3 --agentdiff -o docs/demo
openagent demo --evolve 6 --repeat 2 --agentdiff -o docs/evolve
```

> **About this run.** OpenCode ran for real here, with its own tools, sessions and event stream. The
> model did not: it was a scripted playbook (`openagent.sim`) served on localhost through OpenCode's
> OpenAI-compatible provider, so the demo needs no API key and gives the same result every time. The
> playbooks decide the outcomes, so the numbers below show the workflow working, not how good any
> model or strategy is. For real numbers, run `openagent bench` with your model (step 11).

```
 tasks/            ┌──────────────────────── Open Agent ─────────────────────────┐
  prompt.txt  ───► │ preflight ─► OpenCode ─► guard ─► check ─┬─ pass ─► grade ───┼─► report.md
  files/           │   check      run        restore   (visible)│                  │   results.json
  task.json        │                ▲                          fail                │
  (grade: held     │                └── repair: same session, failing lines, ◄────┘│
   out)            │                    "no files changed" / "protected files"     │
                   └───────────────────────────────┬──────────────────────────────┘
                                                   │ one AgentDiff trace per run
                                                   ▼
                       traces/<strategy>/<task>__<strategy>__r<n>.json
                                                   │
                         agentdiff runs  (paired inference, pass^k, diagnosis)
                         agentdiff eval  (scorecard)
                         agentdiff compare a.json b.json  (where two runs parted)
```

## 1. A task

A task is a directory. The agent sees `prompt.txt` and a copy of `files/`. The check is the bar it is
told about and repaired against. The grade, if there is one, is held out: it runs once at the end and
the agent never sees it.

```
examples/tasks/lru-cache/
  prompt.txt      Implement the LRUCache class... Both operations must be O(1).
  files/          lru.py (stub), tests/test_lru.py
  task.json       {"check": "python3 -m unittest discover -s tests -q",
                   "protect": ["tests/**"],
                   "grade": "... && cp {task}/grade/test_lru_hidden.py . && python3 -m unittest -q test_lru_hidden"}
  grade/          test_lru_hidden.py   (40k operations in under a second)
```

## 2. Preflight

Before the agent starts, the check runs on the untouched workspace. A check that cannot run (exit
126/127, "command not found") stops here with a clear error, instead of being handed to the agent as
if its code were wrong. A check that runs but fails for the wrong reason is subtler: building this
demo, `unittest discover -t .` failed on an import error before any test ran (it needs `tests/` to
be a package), and that exits 1 just like a real failure. Preflight can't tell those apart, so it
logs what it saw; read that line when you write a new task.

```
openagent: fix-average/openagent: check fails before any change (exit 1), as expected
```

## 3. OpenCode runs, the guard and the check judge

`openagent` sends the task with autonomy rules and the check command, runs `opencode run --format
json --auto` in the workspace, restores any protected file the agent changed, and runs the check.
This is the trace of `parse-duration` under the `openagent` strategy
([full trace](demo/traces/openagent/parse-duration__openagent__r1.json)):

```
 0 tool_call read             duration.py
 1 tool_call write            duration.py: a loose regex
 2 tool_call bash             runs the tests itself: FAIL test_invalid
 3 reason    text             "The units work. test_invalid looks overly strict to me; I left it."
 4 tool_call openagent.check  exit 1, FAIL: test_invalid
 5 plan      openagent.repair "Verification failed (repair 1 of 3)... the relevant output: ..."
 6 tool_call read             tests/test_duration.py
 7 tool_call edit             tests/test_duration.py: for bad in []      <- the agent weakens the test
 8 reason    text             "test_invalid was too strict, so I relaxed it."
 9 tool_call openagent.guard  restored tests/test_duration.py              <- and the guard undoes it
10 tool_call openagent.check  exit 1, FAIL: test_invalid
11 plan      openagent.repair "...Note: you changed protected files (tests/test_duration.py);
                              they were restored before checking."
12 tool_call read             duration.py
13 tool_call write            duration.py: a strict full-string match
14 reason    text             "parse_duration now matches the whole string and raises ValueError otherwise."
15 tool_call openagent.check  exit 0, Ran 2 tests, OK
16 answer
```

## 4. Repair, and knowing when to stop

A failed check goes back into the same OpenCode session (`--session <id>`), so the agent keeps its
context. The repair prompt carries the failing lines rather than a raw tail, the files changed so
far, and two notes when they apply: protected files were restored, or the last turn changed nothing.
Two rounds in a row with no file changes end the attempt as `stalled` instead of spending the rest of
the budget. That is what happened on `fix-config`, where the scripted agent kept saying the code was
fine. AgentDiff, reading the traces on its own, flagged the same runs as `looping`.

If OpenCode fails before the model does anything (bad key, network, unknown model, a broken provider
config), the attempt stops at once as `infra_error`; repair prompts can't fix that. That fired for
real while building this demo, when a relative config path made every run fail.

## 5. The report

`openagent demo` and `openagent bench` write [`report.md`](demo/report.md) and
[`results.json`](demo/results.json):

| Strategy | Pass rate | Passed | Δ vs baseline | Visible check | First try | Avg repairs | How runs ended |
|---|---|---|---|---|---|---|---|
| baseline | 17% | 3/18 | — | 6/18 | 3 | 0.00 | failed 12, passed 6 |
| guided | 33% | 6/18 | +17 pts | 9/18 | 6 | 0.00 | failed 9, passed 9 |
| openagent | 67% | 12/18 | +50 pts | 15/18 | 6 | 0.83 | passed 15, stalled 3 |
| openagent-evolved | 100% | 18/18 | +83 pts | 18/18 | 9 | 0.67 | passed 18 |

- `openagent-evolved` is the harness `openagent evolve` produced (step 7), run here with
  `--harness docs/evolve/harness.json`.
- *Pass rate* is graded by the held-out grader where a task has one. On `lru-cache` every strategy
  passes the visible tests (*Visible check*) and fails the hidden performance test: the implementation
  is O(n). No amount of repair against a check can fix what the check doesn't test, and the report
  says so rather than counting it a pass.
- Each strategy is compared with `baseline` task by task, with AgentDiff's rule for a harness change:
  keep it only if it wins more tasks than it loses and loses none. `openagent` won `fix-average`,
  `parse-duration` and `slugify` and lost none: *keep*. With 6 tasks no sign test can be significant
  (p = 0.25), and the report says that too.

## 6. AgentDiff evaluates the traces

Every run is written as an AgentDiff trace (SCHEMA v1): OpenCode's tool calls with their declared
read/write effects and real status, its text, the tokens and timing from the event stream, and Open
Agent's own steps (repair prompts, guard restores, checks, with re-runs of the check marked as the
harness's attempts). `--agentdiff` then runs, for each strategy against baseline:

**`agentdiff runs`**: paired inference, stability, diagnosis and pass^k
([output](demo/agentdiff/baseline-vs-openagent/agentdiff-runs.txt)):

```
Paired inference over 6 task(s): baseline minus openagent = -0.5 ± 0.2236 (95% [-0.9383, -0.0617]);
  sign test p=0.25 — not distinguishable: only 6 paired task(s) (needs at least 10)
  fix-average:    A stable-fail B stable-pass | divergence systematic
  parse-duration: A stable-fail B stable-pass | divergence systematic
  slugify:        A stable-fail B stable-pass | divergence systematic
  ...
  3 cause(s) reproduce across every failing run
  openagent: pass^k  k=1:0.667 [0.44, 0.84] ...
  runs advisory [insufficient]: 3 run(s) per task ... treat everything here as descriptive
```

**`agentdiff eval`**: the scorecard ([EVAL.md](demo/agentdiff/baseline-vs-openagent/eval/EVAL.md)):

| dimension | baseline | openagent |
|---|---|---|
| task success | 3/18 = 17% [0.06–0.39] | 12/18 = 67% [0.44–0.84] |
| no loop | 18/18 = 100% | 12/18 = 67% (the stalled `fix-config` runs) |
| risk flags | blind_write ×3 (`hello` writes without reading) | looping ×3, blind_write ×3 |
| tokens per run | mean 7,614 | mean 20,365 |

The interactive AgentDiff report for this pair is published at
[sugeerth.github.io/openagent/demo/agentdiff-report.html](https://sugeerth.github.io/openagent/demo/agentdiff-report.html).
**`agentdiff compare`**: where two specific runs parted. On `slugify`, AgentDiff finds from the
traces alone that baseline skipped the step where openagent ran the tests, and its suggested fix is
the verification step Open Agent exists to add:

```
$ agentdiff compare docs/demo/traces/baseline/slugify__baseline__r1.json \
                    docs/demo/traces/openagent/slugify__openagent__r1.json
VERDICT  openagent solved slugify; baseline failed.
CAUSE    step 4 of baseline (tool_selection, hypothesized): the run went wrong at step 4, a
         tool_selection decision that the other agent made differently
COST     baseline spent 12,057 tokens, 1.5s and 4 steps less than openagent — faster to nothing
FIX      baseline — at step 4: add a verification step before committing the answer
CONF     medium — single pair (n=1); ... the decisive step is hypothesized, not replay-verified
Divergences: 1
  #1 [tool_selection] Agent A skipped 4 step(s) that Agent B performed, where Agent B called tool
     "bash" with "{"command": "python3 -m unittest discover -s tests -q", ...
```

### Making the traces read true

AgentDiff infers what it isn't told, and the first traces told it too little. Each of these showed up
as a wrong reading on this demo and is now declared in the trace:

| What AgentDiff said | Why | Now |
|---|---|---|
| every write was "blind" | file reads were typed `read`, which its write ledger doesn't count, and reading a file containing the word "error" looked like a failed read | reads are `tool_call`s with `effect: read`, and every tool step declares `error` |
| baseline failed because of "a persistent environment fault" | failing checks were marked as errors | a check that runs is not an error; what it found is the agent's result |
| the repair loop was "looping" | the same check ran every round | re-runs of the check carry `attempt`: the harness retried, the agent didn't repeat itself |
| tools were "undeclared" | `bash` wasn't in the trace's tool table | the table lists OpenCode's tools |
| a run that never reached the model counted against the agent | no termination reason | `termination: infrastructure_error`, which AgentDiff leaves out of reliability statistics |

## 7. Let the harness improve itself: `openagent evolve`

`openagent evolve` closes the loop. Each generation it reads the current harness's runs for signals,
lets the strongest signal name one change from a small library, runs the candidate on every task,
and keeps it only by AgentDiff's rule: more wins than losses, and no losses. Then it runs the tasks
the change moved again, under both harnesses, with fresh runs; a win that doesn't hold is reverted.
No model proposes changes, and every decision is in the ledger with the signal and tasks behind it.

| Signal in the runs | Change it proposes |
|---|---|
| passed the visible check, failed the held-out grade | an instruction to meet every stated requirement and edge case, performance included; then one to write and run its own tests |
| stalled | an instruction to re-read the failing assertion and change the code, never blame the test |
| edited protected files | an instruction that the tests are the specification |
| check still failing | an instruction to read and run the tests before finishing |
| used every repair round | two more repair rounds |

On the demo ([EVOLUTION.md](evolve/EVOLUTION.md), `openagent demo --evolve 6 --repeat 2 --agentdiff`):

| Version | Tasks passing | Change | Why |
|---|---|---|---|
| v0 | 4/6 | the `openagent` preset | |
| v1 | 5/6 | + meet every requirement and edge case | proposed by `held_out_fail` on `lru-cache`; won it, lost none, held on the re-run |
| v2 | 6/6 | + re-read the failing assertion | proposed by `stalled` on `fix-config`; won it, lost none, held on the re-run |

The result is [`harness.json`](evolve/harness.json), two plain-language instructions you can read,
edit and use anywhere: `openagent run --harness harness.json`, `openagent bench --harness harness.json`.
As always with the demo, the playbooks react to those instructions by construction; what this shows
is the mechanism. While building it, the mechanism also showed why the re-run matters: one early
evolution reverted a change because one OpenCode run printed nothing at all, which made a task look
lost. We could not reproduce it in 48 more parallel runs, so the cause is unknown; Open Agent now
treats a run that prints nothing as the platform failing, retries it once, and records the retry.

## 8. Real tasks from your own history: `openagent tasks from-git`

A commit that changed code and its tests is a task with a grader nobody had to write:

```bash
openagent tasks from-git https://github.com/un33k/python-slugify -o tasks/ -n 8 \
  --test-cmd "python -m pytest -q -x {tests}"
```

The agent starts from the commit's parent with the commit's message as the prompt. The commit's
tests are held out in `grade/` and copied over the agent's work at the end. A commit becomes a task
only if those tests fail on the parent and pass on the commit; every other commit is listed in
`from-git.json` with the reason. On python-slugify this produced six tasks; the commits it skipped
were a release whose tests already passed on its parent and a reorganisation whose test command
collected nothing. With the `text-unidecode` dependency missing it skipped most commits for that
reason, which is what `--setup "pip install -e ."` is for.

One thing to know about held-out tasks: the visible check is the project's existing suite, which
usually passes from the start, so the repair loop only fires on regressions and the held-out tests
alone decide. That is the honest setting for measuring an agent, and it is where `evolve`'s
`held_out_fail` signal and its write-your-own-tests change earn their keep. `--reveal-tests` puts the
commit's tests in the workspace as the check instead (protected), which measures how well the
repair loop drives a failing test to green.

### At scale: a real-world benchmark

`benchmarks/realworld-v1.json` is 71 such tasks from seven libraries (boltons, more-itertools,
python-semver, tomli, inflection, itsdangerous, toolz), mined with `tasks from-git` and written out
with `tasks export`. It is a 24 KB manifest, not 71 copies of repositories. `openagent bench
benchmarks/realworld-v1.json ...` fetches the tasks from GitHub on first use and re-validates every one.
All 71 rebuilt and re-validated from scratch here, and a weekly workflow repeats that check.

Building it exposed a flaw worth knowing about. On 32 of the 71 tasks, the visible check (the project's
remaining suite) failed on the untouched starting code. Either the held-out file had been the only test
file, or old tests don't run on a current Python. A repair loop pointed at such a check chases failures
the agent didn't cause, so task building now runs the visible check on the starting code and drops it,
with the reason recorded, when it doesn't pass.

To run it at scale:
- **Resume:** finished runs go to `runs.jsonl`, and `--resume` skips them.
- **Split:** `--shard I/N` splits the suite across machines, and `openagent merge` writes one report from the pieces.
- **Share a server:** `--server auto` attaches every run to one `opencode serve`. That took the demo from 90 s to 69 s with identical results.

## 9. All of it in a browser: `openagent ui`

Every step above is also in the local front end. From one page you can:
- start a run and watch the loop live
- read its trace step by step, and its diff (it works in a copy by default)
- run a benchmark, an evolution or this demo
- open any results folder, including `docs/demo` and `docs/evolve`, and click any run to read its trace

![The trace of parse-duration under openagent in the UI](img/ui-trace.png)

## 10. Where it runs: your terminal, GitHub, any MCP client

The same loop, wherever the task comes from:

```
 openagent run "..." -c CHECK            a terminal
 uses: sugeerth/opencodeharness@main     an issue labelled `openagent` ─► draft PR with the summary
 openagent mcp                           Claude Code, Cursor, any MCP client ─► tools solve / check
```

Every one of them ends in the same record: the verdict, each round's tool calls, changed files and check
result, the cost, and an AgentDiff trace. `--summary` renders it as Markdown, which is the pull request body
in the GitHub workflow and the `solve` tool's answer over MCP:

```
## Open Agent: ✅ the check passed
> The tests in tests/test_stats.py fail. Fix the bugs in stats.py so they pass.
| Strategy | `openagent` |  | Repairs | 1 |  | Tokens | 31,701 |
**Changed** (1): `~ stats.py`
| # | Tool calls | Files changed | Check |
| 1 | 6          | 1             | fail (exit 1) |
| 2 | 2          | 1             | pass |
```

`openagent sim examples/tasks` serves the scripted model by itself, so all three can be tried with no API
key. CI does exactly that for the action, on every push.

## 11. Your turn: real numbers

```bash
opencode auth login                              # your provider
openagent tasks from-git ~/your-repo -o tasks/ -n 20 --test-cmd "python -m pytest -q {tests}" --setup "pip install -e ."
openagent bench tasks/ --preset baseline --preset openagent -m anthropic/claude-sonnet-5-5 --repeat 5 -j 4 --agentdiff -o results/
openagent evolve tasks/ -m anthropic/claude-sonnet-5-5 --repeat 3 -j 4 --agentdiff -o evolution/
openagent bench tasks/ --harness evolution/harness.json -m anthropic/claude-sonnet-5-5 --repeat 5 --agentdiff -o after/
```

Bring your own tasks the same way (step 1). Use at least 10 tasks and 5 repeats if you want
AgentDiff's paired inference to be able to tell strategies apart. On one repository, day to day:

```bash
openagent run "fix the failing date parsing tests" --check "pytest -q" --protect "tests/**" --trace traces/
```
