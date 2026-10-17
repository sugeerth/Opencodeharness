# The workflow, end to end

Open Agent is an agent that runs on top of [OpenCode](https://github.com/sst/opencode): OpenCode does
the work, and Open Agent decides when the work is done. Every run can be evaluated by
[AgentDiff](https://github.com/sugeerth/Agenttracing). This page walks through the whole loop on the
run committed in [`docs/demo`](demo/), which you can reproduce with one command:

```bash
git clone https://github.com/sugeerth/Opencodeharness && cd Opencodeharness
pip install -e . git+https://github.com/sugeerth/Agenttracing
npm i -g opencode-ai
openagent demo --repeat 3 --agentdiff -o docs/demo
```

> **About this run.** OpenCode ran for real here, with its own tools, sessions and event stream. The
> model did not: it was a scripted playbook (`openagent.sim`) served on localhost through OpenCode's
> OpenAI-compatible provider, so the demo needs no API key and gives the same result every time. The
> playbooks decide the outcomes, so the numbers below show the workflow working, not how good any
> model or strategy is. For real numbers, run `openagent bench` with your model (step 6).

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

## 7. Your turn: real numbers

```bash
opencode auth login                              # your provider
openagent bench examples/tasks --preset baseline --preset openagent --preset best-of-3 \
  -m anthropic/claude-sonnet-5-5 --repeat 5 -j 4 --agentdiff -o results/
```

Bring your own tasks the same way (step 1). Use at least 10 tasks and 5 repeats if you want
AgentDiff's paired inference to be able to tell strategies apart. On one repository, day to day:

```bash
openagent run "fix the failing date parsing tests" --check "pytest -q" --protect "tests/**" --trace traces/
```
