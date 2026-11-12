# Benchmarks

The cost thesis is measured, not asserted. Each number below names its
source, so you can check it yourself.

## SWE-bench Lite (cost and quality)

A controlled 3-arm study. The model ceiling is the same in every arm. Only
the orchestration varies:

- **A:** frontier baseline. Every turn runs on the ceiling model (gpt-5.5).
- **B:** A + CodeGraph context injection + task-aware model router.
- **C:** B + durable retries.

Lite-300 run, 135 submitted tasks per arm, one seed
(`runs_swe_lite/merged_report.json` in the study repo):

| Arm | Resolved | Actual spend | Cost per resolve |
|---|---|---|---|
| A | 98 / 135 | $27.51 | $0.281 |
| B | 80 / 135 | $17.44 | $0.218 |
| C | 90 / 135 | $19.45 | $0.216 |

- Arm C spends 29% less than arm A and resolves 8 fewer tasks.
- Arm B spends 37% less than arm A and resolves 18 fewer tasks.
- The n=20 pilot was 5 / 20 resolved for both A and B, at $15.83 and
  $8.51. That is where the older "about 47% cheaper at equal quality" line
  came from. Lite-300 does not show equal quality.
- One seed only. Quality parity is not proven.

The frozen predictions grade again deterministically with only Docker (no
API keys): [swebench-pm](https://github.com/professorpalmer/swebench-pm).

## NL2Repo-Bench (build a library from a spec)

Durable-state orchestration reaches a **91.1% mean test-pass rate, about
2.28x the ~40% published state of the art**. It solves 53% of libraries to
a fully green upstream suite. The full method and caveats are in the paper:
[durable-state-vs-context](https://professorpalmer.github.io/durable-state-vs-context/)
([DOI](https://doi.org/10.5281/zenodo.20709565)).

## Related

- [Findings](findings.md): which models can drive the harness (research rig).
- [Router evaluation](router-evaluation.md): recorded router comparison on heldout analysis tasks.
