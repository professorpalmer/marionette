# pmharness: the research rig

Marionette started as an eval.
One question: which LLMs can drive Puppetmaster as a native harness layer?
That rig lives here and stays green.
It is not the shipping GUI contract. The product is in [`harness/`](../harness/README.md).

| Module | Role |
|---|---|
| `intent.py` | DriverIntent contract + strict validator + lenient text-to-JSON parser. Pure, no PM dependency. |
| `bridge.py` | Runs a validated intent against Puppetmaster's in-process Orchestrator (local adapter: deterministic, free). Also owns the worker token budget parse. |
| `drivers/` | Driver protocol: StubDriver (offline oracle), OpenAICompatDriver, plus Anthropic, Gemini, Bedrock, Codex Responses, and CLI drivers. The product pilot uses the same drivers. |
| `registry.py` + `catalog.json` | Data-driven model catalog (license, price, vision, tier). |
| `scoring.py` / `scoring_v2.py` | Deterministic scoring, no LLM-as-judge: json_valid, schema_valid, action_correct, executed_ok, composite. |
| `battery.py`, `runner*.py`, `episode*.py` | The Stage 1-4 eval batteries and runners. |
| `analysis_bench.py` | Exact-fact analysis scorer, used by the [router evaluation](../docs/evidence/router-evaluation.md). |
| `ledger.py` | Append-only SQLite record of every attempt. |

Driver eval grades **driving**, not **working**. Swarm intents run on
Puppetmaster's free local adapter, so ground truth is deterministic and
needs no keys.

Run it offline:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/run_eval.py --drivers stub-oracle
```

Results and the stage ladder: [Findings](../docs/evidence/findings.md) and
[Architecture section 8](../docs/design/architecture.md#8-research-rig-pmharness-and-the-evaluation-ladder).
