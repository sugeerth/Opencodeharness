# Open Agent benchmark

> **Synthetic run.** OpenCode ran for real, with its own tools and sessions, but the model was a scripted playbook (openagent.sim), not an LLM. These numbers show the workflow, not how well any model or strategy does: the playbooks decide that.

- Model: `scripted/coder`
- Tasks: 6, repeats: 3
- Date: 2026-10-17 23:14

| Strategy | Pass rate | Passed | Δ vs baseline | Visible check | First try | Avg repairs | Avg cost | Avg tokens | Avg time | How runs ended |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 17% | 3/18 | — | 6/18 | 3 | 0.00 | $0.0000 | 7,614 | 5s | failed 12, passed 6 |
| guided | 33% | 6/18 | +17 pts | 9/18 | 6 | 0.00 | $0.0000 | 12,969 | 5s | failed 9, passed 9 |
| openagent | 67% | 12/18 | +50 pts | 15/18 | 6 | 0.83 | $0.0000 | 20,365 | 9s | passed 15, stalled 3 |
| openagent-evolved | 100% | 18/18 | +83 pts | 18/18 | 9 | 0.67 | $0.0000 | 22,576 | 8s | passed 18 |

Pass rate is graded by held-out tests the agent never saw where a task has them; *Visible check* is the check the agent could see and was repaired against.

## Against baseline, task by task

AgentDiff's rule for a harness change: keep it only if it wins more tasks than it loses and loses none.

| Strategy | Wins | Losses | Ties | Sign test p | Verdict |
|---|---|---|---|---|---|
| guided | slugify | — | 5 | 1.000 | keep |
| openagent | fix-average, parse-duration, slugify | — | 3 | 0.250 | keep |
| openagent-evolved | fix-average, fix-config, lru-cache, parse-duration, slugify | — | 1 | 0.062 | keep |

_fewer than 10 tasks: no sign test can be significant here._

## Per task

| Task | baseline | guided | openagent | openagent-evolved |
|---|---|---|---|---|
| fix-average | 0/3 | 0/3 | 3/3 | 3/3 |
| fix-config | 0/3 | 0/3 | 0/3 | 3/3 |
| hello | 3/3 | 3/3 | 3/3 | 3/3 |
| lru-cache | 0/3 (check 3/3) | 0/3 (check 3/3) | 0/3 (check 3/3) | 3/3 |
| parse-duration | 0/3 | 0/3 | 3/3 | 3/3 |
| slugify | 0/3 | 3/3 | 3/3 | 3/3 |

## AgentDiff evaluation

### baseline-vs-guided

```
Agents: A=baseline  B=guided
Paired inference over 6 task(s): baseline minus guided = -0.1667 ± 0.1667 (95% [-0.4933, 0.16]); sign test p=1.0 — not distinguishable: only 6 paired task(s) (needs at least 10)
Runs: {'baseline': 3, 'guided': 3}
  fix-average: A stable-fail B stable-fail | divergence systematic (tool_selection, rate 1)
  fix-config: A stable-fail B stable-fail | divergence systematic (tool_selection, rate 1)
  hello: A stable-pass B stable-pass | divergence none
  lru-cache: A stable-fail B stable-fail | divergence systematic (tool_selection, rate 1)
  parse-duration: A stable-fail B stable-fail | divergence systematic (tool_selection, rate 1)
  slugify: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
baseline fails all runs on 5 task(s) (reproducible); guided fails all runs on 4 task(s) (reproducible). Divergences are systematic on 5 of 6 task(s), so the observed gaps are behavioral, not noise.
Diagnosis across runs:
  fix-average / baseline (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for baseline on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  fix-average / guided (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for guided on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  fix-config / baseline (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for baseline on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  fix-config / guided (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for guided on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  lru-cache / baseline (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for baseline on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  lru-cache / guided (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for guided on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  parse-duration / baseline (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for baseline on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  parse-duration / guided (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for guided on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  slugify / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  1 cause(s) reproduce across every failing run
Reliability (repeated runs):
  baseline: 3/18 run(s) succeeded across 6 task(s); max_k=3
    pass^k  k=1:0.167 [0.06, 0.39]  k=2:0.167 [0.00, 0.15]  k=3:0.167 [0.00, 0.06]
    pass@k  k=1:0.167  k=2:0.167  k=3:0.167
    interval: plug-in: the 95% Wilson interval of the pooled per-run success rate, raised to k; treats runs as exchangeable across tasks
    outcome   consistency: 1.000 over 6/6 task(s)
    trajectory consistency: 1.000 over 1/6 task(s)
    resources  consistency: 0.982 over 6/6 task(s)
    ICC(1): 1.000 (0% of variance is within-task, i.e. the agent itself)
    runs advisory [insufficient]: 3 run(s) per task at the thinnest task (median 3, max 3). That is below the 5-run mark where a difference between two agents starts to mean anything: treat everything here as descriptive of these runs only. 8-16 runs is the defensible floor for structured tool-use tasks, 32+ for open-ended reasoning.
```

Full outputs: `agentdiff/baseline-vs-guided/runs/report.html` and the scorecard in `agentdiff/baseline-vs-guided/eval`

### baseline-vs-openagent

```
Agents: A=baseline  B=openagent
Paired inference over 6 task(s): baseline minus openagent = -0.5 ± 0.2236 (95% [-0.9383, -0.0617]); sign test p=0.25 — not distinguishable: only 6 paired task(s) (needs at least 10)
Runs: {'baseline': 3, 'openagent': 3}
  fix-average: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
  fix-config: A stable-fail B stable-fail | divergence systematic (tool_selection, rate 1)
  hello: A stable-pass B stable-pass | divergence none
  lru-cache: A stable-fail B stable-fail | divergence systematic (tool_selection, rate 1)
  parse-duration: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
  slugify: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
baseline fails all runs on 5 task(s) (reproducible); openagent fails all runs on 2 task(s) (reproducible). Divergences are systematic on 5 of 6 task(s), so the observed gaps are behavioral, not noise.
Diagnosis across runs:
  fix-average / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  fix-config / baseline (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for baseline on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  fix-config / openagent (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for openagent on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  lru-cache / baseline (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for baseline on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  lru-cache / openagent (fails 3 of 3): [all_contested] every diagnosed run was contested — the evidence never picks a cause for openagent on this task
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  parse-duration / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  slugify / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  3 cause(s) reproduce across every failing run
Reliability (repeated runs):
  baseline: 3/18 run(s) succeeded across 6 task(s); max_k=3
    pass^k  k=1:0.167 [0.06, 0.39]  k=2:0.167 [0.00, 0.15]  k=3:0.167 [0.00, 0.06]
    pass@k  k=1:0.167  k=2:0.167  k=3:0.167
    interval: plug-in: the 95% Wilson interval of the pooled per-run success rate, raised to k; treats runs as exchangeable across tasks
    outcome   consistency: 1.000 over 6/6 task(s)
    trajectory consistency: 1.000 over 1/6 task(s)
    resources  consistency: 0.982 over 6/6 task(s)
    ICC(1): 1.000 (0% of variance is within-task, i.e. the agent itself)
    runs advisory [insufficient]: 3 run(s) per task at the thinnest task (median 3, max 3). That is below the 5-run mark where a difference between two agents starts to mean anything: treat everything here as descriptive of these runs only. 8-16 runs is the defensible floor for structured tool-use tasks, 32+ for open-ended reasoning.
  openagent: 12/18 run(s) succeeded across 6 task(s); max_k=3
    pass^k  k=1:0.667 [0.44, 0.84]  k=2:0.667 [0.19, 0.70]  k=3:0.667 [0.08, 0.59]
    pass@k  k=1:0.667  k=2:0.667  k=3:0.667
    interval: plug-in: the 95% Wilson interval of the pooled per-run success rate, raised to k; treats runs as exchangeable across tasks
```

Full outputs: `agentdiff/baseline-vs-openagent/runs/report.html` and the scorecard in `agentdiff/baseline-vs-openagent/eval`

### baseline-vs-openagent-evolved

```
Agents: A=baseline  B=openagent-evolved
Paired inference over 6 task(s): baseline minus openagent-evolved = -0.8333 ± 0.1667 (95% [-1.16, -0.5067]); sign test p=0.0625 — not distinguishable: only 6 paired task(s) (needs at least 10)
Runs: {'baseline': 3, 'openagent-evolved': 3}
  fix-average: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
  fix-config: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
  hello: A stable-pass B stable-pass | divergence none
  lru-cache: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
  parse-duration: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
  slugify: A stable-fail B stable-pass | divergence systematic (tool_selection, rate 1)
baseline fails all runs on 5 task(s) (reproducible); openagent-evolved is stable on every task. Divergences are systematic on 5 of 6 task(s), so the observed gaps are behavioral, not noise.
Diagnosis across runs:
  fix-average / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  fix-config / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  lru-cache / baseline (fails 3 of 3): [confirmed] baseline run r1 failed with an answer 0.90 similar (token Jaccard) to openagent-evolved run r1's *passing* answer — the grader treated near-identical answers differently
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  parse-duration / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  slugify / baseline (fails 3 of 3): [reproducible] divergence leads the diagnosis in all 3 diagnosed runs — the cause reproduces, not just the failure
    spectrum: spectrum needs both classes; this agent has 3 failing and 0 passing run(s)
  1 diagnosis(es) confirmed by executed checks against the corpus itself — no re-run needed; 4 cause(s) reproduce across every failing run
Reliability (repeated runs):
  baseline: 3/18 run(s) succeeded across 6 task(s); max_k=3
    pass^k  k=1:0.167 [0.06, 0.39]  k=2:0.167 [0.00, 0.15]  k=3:0.167 [0.00, 0.06]
    pass@k  k=1:0.167  k=2:0.167  k=3:0.167
    interval: plug-in: the 95% Wilson interval of the pooled per-run success rate, raised to k; treats runs as exchangeable across tasks
    outcome   consistency: 1.000 over 6/6 task(s)
    trajectory consistency: 1.000 over 1/6 task(s)
    resources  consistency: 0.982 over 6/6 task(s)
    ICC(1): 1.000 (0% of variance is within-task, i.e. the agent itself)
    runs advisory [insufficient]: 3 run(s) per task at the thinnest task (median 3, max 3). That is below the 5-run mark where a difference between two agents starts to mean anything: treat everything here as descriptive of these runs only. 8-16 runs is the defensible floor for structured tool-use tasks, 32+ for open-ended reasoning.
  openagent-evolved: 18/18 run(s) succeeded across 6 task(s); max_k=3
    pass^k  k=1:1.000 [0.82, 1.00]  k=2:1.000 [0.68, 1.00]  k=3:1.000 [0.56, 1.00]
    pass@k  k=1:1.000  k=2:1.000  k=3:1.000
    interval: plug-in: the 95% Wilson interval of the pooled per-run success rate, raised to k; treats runs as exchangeable across tasks
    outcome   consistency: 1.000 over 6/6 task(s)
    trajectory consistency: 1.000 over 6/6 task(s)
    resources  consistency: 0.984 over 6/6 task(s)
    ICC(1): n/a (every eligible run has the same outcome, so there is no variance to partition)
```

Full outputs: `agentdiff/baseline-vs-openagent-evolved/runs/report.html` and the scorecard in `agentdiff/baseline-vs-openagent-evolved/eval`

