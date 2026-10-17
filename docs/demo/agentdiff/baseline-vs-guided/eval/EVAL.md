# Evaluation scorecard

Mode: online — traces as recorded

| dimension | baseline | guided |
|---|---|---|
| task success | 3/18 = 17% [0.06–0.39] | 6/18 = 33% [0.16–0.56] |
| correct tool called | — (not measurable) | — (not measurable) |
| useful tool results (over calls) | 0/54 = 0% [0.00–0.07] | 0/84 = 0% [0.00–0.04] |
| expected evidence retrieved (golden) | — (not measurable) | — (not measurable) |
| answer grounded | — (not measurable) | — (not measurable) |
| policy compliant | — (not measurable) | — (not measurable) |
| no risk flag | 15/18 = 83% [0.61–0.94] | 15/18 = 83% [0.61–0.94] |
| stopped when done | — (not measurable) | — (not measurable) |
| no loop | 18/18 = 100% [0.82–1.00] | 18/18 = 100% [0.82–1.00] |
| no tool error | 18/18 = 100% [0.82–1.00] | 18/18 = 100% [0.82–1.00] |
| every milestone reached (golden) | — (not measurable) | — (not measurable) |
| milestones reached in order (golden) | — (not measurable) | — (not measurable) |
| errors recovered (over errors) | — (not measurable) | — (not measurable) |
| milestones reached (over milestones) | — (not measurable) | — (not measurable) |
| latency (s) per run | mean 6.0847 (median 6.079, 4.627–6.915) | mean 6.7127 (median 6.769, 5.806–7.677) |
| wasted seconds per run | mean 0.3226 (median 0.272, 0.005–1.26) | mean 0.8178 (median 0.79, 0.009–1.853) |
| share of time waiting on tools per run | mean 0.9976 (median 1, 0.9875–1) | mean 0.9944 (median 1, 0.9–1) |
| cost (USD) per run | mean 0 (median 0, 0–0) | mean 0 (median 0, 0–0) |
| tokens per run | mean 7614.17 (median 7583.5, 4847–10395) | mean 12969.2 (median 10919, 5121–20479) |
| steps per run | mean 5 (median 5, 4–6) | mean 6.6667 (median 6, 4–9) |
| tool calls per run | mean 3 (median 3, 2–4) | mean 4.6667 (median 4, 2–7) |
| accuracy score (outcome.score) per run | mean 0.1667 (median 0, 0–1) | mean 0.3333 (median 0, 0–1) |
| risk vs reward | reward 17% · risk 17% · ratio 1 | reward 33% · risk 17% · ratio 2 |
| retrieval and time | 0/54 calls fed the answer, 0 returned nothing, 54 dead ends; 100% of time wasted | 0/84 calls fed the answer, 6 returned nothing, 78 dead ends; 100% of time wasted |
| milestones | — (no task names milestones) | — (no task names milestones) |
| risk flags | 3 flag(s) in 3 run(s): blind_write ×3 | 3 flag(s) in 3 run(s): blind_write ×3 |
| LLM judge | no judge | no judge |

every rate is successes/runs with a 95% Wilson interval (useful tool results over calls, expected evidence over the golden list, recovered errors over errors); 'correct tool', 'expected evidence' and 'policy compliant' need a golden set or a policy and read None without one; write/read effects are declared by the tool or inferred from its name (the basis is stated per run); the judge's verdicts are reported beside the grade, never merged into it
