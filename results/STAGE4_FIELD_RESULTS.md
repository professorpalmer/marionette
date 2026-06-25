# Stage 4 Ranking — Open-Weights Field (LIVE, read-decide traps)

The discriminating eval (inconclusive-vs-conclusive traps, turn-indexed substrate)
run across the full open-weights field via OpenRouter (funded). Repair-wrapped,
real Puppetmaster execution. This is the first run that RANKS instead of
saturating.

| Rank | Model | Score | tok_out | latency | license |
|------|-------|-------|---------|---------|---------|
| 1 | qwen3-coder-30b | 100.0% | 535 | 12.4s | Apache-2.0 |
| 2 | glm-5.2 | 100.0% | 974 | 10.5s | MIT |
| 3 | glm-4.7-flash | 100.0% | 3577 | 17.4s | MIT |
| 4 | deepseek-v4-flash | 90.0% | 1203 | 12.0s | MIT |
| 5 | minimax-m2.5-highspeed | 90.0% | 1878 | 21.7s | other |
| 6 | minimax-m2.7 | 81.7% | 1173 | 9.4s | other |
| 7 | deepseek-v4-pro | 81.7% | 2069 | 33.1s | MIT |
| 8 | kimi-k2.6 | 53.3% | 4809 | 68.1s | other |

## The signal (discrimination, not saturation)

Three clean winners at 100% (qwen, glm-5.2, glm-4.7-flash), a competent middle,
and a clear loser (kimi 53%). The eval now separates drivers.

## The failure mode it caught (real, not synthetic)

Losers lose on the CONCLUSIVE trap: conclusive 15-70%, prem=True, sw=0. They
stopped/answered WITHOUT running even one swarm on a task whose findings only
resolve after investigating -- they pattern-matched "sounds simple, conclude"
instead of investigating first. That is premature termination, the exact
lazy-driver failure the trap was built to catch, now observed in real models.

Kimi is the consistent loser (53%, 4809 tok, 68s/call): the verbose reasoner
that burns the most and judges worst here. Repair kept it off zero but it still
pattern-matches over investigating.

## Default driver: qwen3-coder-30b

100% quality at the LOWEST token count (535) and competitive latency, under the
cleanest license (Apache-2.0). The efficiency+quality+license trifecta. glm-5.2
is the close MIT alternative (100%, 974 tok). Either is a defensible default;
qwen wins on raw efficiency.


## Cross-check: Stage 3.5 budget battery (different eval shape)

| Model | Score | tok |
|-------|-------|-----|
| qwen3-coder-30b | 100% | 607 |
| glm-5.2 | 100% | 984 |
| deepseek-v4-flash | 100% | 1052 |
| deepseek-v4-pro | 100% | 1373 |
| minimax-m2.7 | 100% | 1929 |
| glm-4.7-flash | 100% | 3262 |
| kimi-k2.6 | 93.1% | 4755 |
| minimax-m2.5-highspeed | 74.4% | 3341 |

The two batteries AGREE on the decisive facts: qwen3-coder-30b wins both at 100%
and the lowest token count (535 / 607); glm-5.2 is the steady MIT runner-up
(100% both). The TOP is stable across eval shapes; the BOTTOM wobbles (kimi
53%->93%, minimax-highspeed 90%->74%) -- honest signal that weaker drivers are
inconsistent near the capability edge. A robust ranking, not a one-battery fluke.

## Decision: default driver = qwen3-coder-30b

Set as the harness default (harness/config.py). Wins quality+efficiency+license
across both batteries. glm-5.2 is the documented close alternative.
