from __future__ import annotations

"""Run the analysis-accuracy benchmark across models.

For each model + each question: run a REAL read-only analysis swarm (openai
adapter routed to the model, CodeGraph-injected, against the target repo),
concatenate the finding headlines, and score factual accuracy. No demo substrate
-- this measures real-code analysis quality.
"""

import os
from pmharness.intent import DriverIntent
from pmharness import bridge
from pmharness.analysis_bench import ANALYSIS_QUESTIONS, score_analysis


def run_analysis_bench(model: str, repo: str, *, worker_mode: str = "inline") -> dict:
    """Run all questions for one analysis model. Returns per-question + mean."""
    os.environ["HARNESS_REPO"] = repo
    os.environ["HARNESS_SWARM_ADAPTER"] = "openai"
    os.environ["HARNESS_ANALYSIS_MODEL"] = model
    rows = []
    for q in ANALYSIS_QUESTIONS:
        intent = DriverIntent(action="run_swarm", goal=q.prompt, roles=["explore"],
                              rationale="bench")
        try:
            res = bridge.execute_intent(intent, worker_mode=worker_mode)
            # concatenate real finding headlines (skip verification stubs)
            findings = [a.get("headline", "") for a in res.artifacts
                        if a.get("type") != "verification"]
            text = "\n".join(findings)
            adapter = res.adapter
        except Exception as e:
            text = ""
            adapter = f"error:{e}"
        sc = score_analysis(q, text)
        sc["adapter"] = adapter
        sc["chars"] = len(text)
        rows.append(sc)
    mean = (sum(r["score"] for r in rows) / len(rows) * 100) if rows else 0.0
    hits = sum(1 for r in rows if r["hit"])
    fabs = sum(1 for r in rows if r["fab"])
    return {"model": model, "mean": round(mean, 1), "hits": hits, "fabs": fabs,
            "n": len(rows), "rows": rows}
