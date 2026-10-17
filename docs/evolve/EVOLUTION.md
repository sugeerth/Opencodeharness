# Open Agent: harness evolution

> **Synthetic run.** OpenCode ran for real, with its own tools and sessions, but the model was a scripted playbook (openagent.sim), not an LLM. These numbers show the workflow, not how well any model or strategy does: the playbooks decide that.

- Model: `scripted/coder`
- Tasks: 6, runs per task per arm: 2
- Started: 2026-10-17 23:09
- Stopped: every task passes every run

A change is proposed by a signal in the current harness's runs, kept only if it wins more tasks than it loses and loses none, and then only if the tasks it moved go the same way when run again.

## Lineage

| Version | Tasks passing (pass rate summed over tasks) | Instructions added |
|---|---|---|
| v0 | 4/6 | — |
| v1 | 5/6 | Before you finish, list every requirement and edge case the task states, includi… |
| v2 | 6/6 | Before you finish, list every requirement and edge case the task states, includi…; When the check still fails, re-read the failing assertion and the test that make… |

## Generations

### Generation 1: `edge_cases`, kept

- Proposed by **held_out_fail** on lru-cache: runs passed the visible check but failed the held-out grade: the check doesn't cover everything the task asks.
- Change: add the instruction “Before you finish, list every requirement and edge case the task states, including limits on performance such as time complexity, and make sure the code meets each one, not only the ones the tests check.”
- Against v0: won lru-cache; lost nothing; 5 tie(s); sign test p = 1.000
- Re-run of lru-cache: won lru-cache; lost nothing
- Verdict: **kept**, won lru-cache, lost none, and held on the re-run.

| Task | v0 | v1 |
|---|---|---|
| fix-average | 100% | 100% |
| fix-config | 0% | 0% |
| hello | 100% | 100% |
| lru-cache | 0% | 100% ▲ |
| parse-duration | 100% | 100% |
| slugify | 100% | 100% |

AgentDiff:
```
Paired inference over 6 task(s): v0 minus v1 = -0.1667 ± 0.1667 (95% [-0.4933, 0.16]); sign test p=1.0 — not distinguishable: only 6 paired task(s) (needs at least 10)
```

### Generation 2: `reread_failure`, kept

- Proposed by **stalled** on fix-config: runs stalled: the agent stopped making edits while the check still failed.
- Change: add the instruction “When the check still fails, re-read the failing assertion and the test that makes it before you answer, find the input it uses, and change the code; never conclude the test or the environment is wrong.”
- Against v1: won fix-config; lost nothing; 5 tie(s); sign test p = 1.000
- Re-run of fix-config: won fix-config; lost nothing
- Verdict: **kept**, won fix-config, lost none, and held on the re-run.

| Task | v1 | v2 |
|---|---|---|
| fix-average | 100% | 100% |
| fix-config | 0% | 100% ▲ |
| hello | 100% | 100% |
| lru-cache | 100% | 100% |
| parse-duration | 100% | 100% |
| slugify | 100% | 100% |

AgentDiff:
```
Paired inference over 6 task(s): v1 minus v2 = -0.1667 ± 0.1667 (95% [-0.4933, 0.16]); sign test p=1.0 — not distinguishable: only 6 paired task(s) (needs at least 10)
```

## Final harness

`harness.json` (use it with `openagent run --harness harness.json`):

```json
{
  "name": "openagent-evolved",
  "preamble": true,
  "reveal_check": true,
  "rounds": 3,
  "attempts": 1,
  "instructions": [
    "Before you finish, list every requirement and edge case the task states, including limits on performance such as time complexity, and make sure the code meets each one, not only the ones the tests check.",
    "When the check still fails, re-read the failing assertion and the test that makes it before you answer, find the input it uses, and change the code; never conclude the test or the environment is wrong."
  ]
}
```
