from __future__ import annotations

"""Analysis-accuracy benchmark.

The Stage 1-4 ladder measured DRIVER judgment (swarm vs answer vs stop). This
measures something different and previously UNMEASURED: when the harness runs a
real read-only analysis swarm, how FACTUALLY ACCURATE are the findings? Earlier
dogfooding found a cheap 30B model reads the right files but fabricates specifics
(wrong method names, wrong return types). This benchmark turns that anecdote into
a number so we can set a defensible default analysis model and state its bound.

Each question has deterministic ground truth checkable against finding text:
  must_contain     -- substrings, ANY of which present == the correct fact found
  must_not_contain -- fabrications actually observed; presence == a hallucination

Scoring per question:
  hit   = any(must_contain)      (found the right fact)
  fab   = any(must_not_contain)  (asserted a known-wrong fact)
  score = 1.0 if (hit and not fab) else 0.5 if (hit and fab) else
          0.0 if (not hit and fab) else 0.25  (silent: no hit, no fab)

Ground truth is anchored to the REAL pm-harness source (verified at authoring).
"""

from dataclasses import dataclass, field


@dataclass
class AnalysisQ:
    id: str
    prompt: str            # the investigation brief handed to the analysis swarm
    must_contain: tuple    # correct-fact substrings (lowercased match)
    must_not_contain: tuple = ()  # known fabrications (lowercased match)


# Anchored to real source (pm-harness), verified at authoring time.
ANALYSIS_QUESTIONS = (
    AnalysisQ(
        id="repair_fn",
        prompt=("In harness/repair.py, name the single public function that wraps a "
                "driver call with a repair retry, and state how many repairs it does "
                "by default. Report the exact function name and the default count."),
        must_contain=("drive_with_repair",),
        must_not_contain=("repair_intent", "3 retries", "three retries", "5 retries"),
    ),
    AnalysisQ(
        id="repair_default",
        prompt=("In harness/repair.py drive_with_repair, what is the DEFAULT value of "
                "the max_repairs parameter? State the number."),
        must_contain=("max_repairs", "1"),
        must_not_contain=("max_repairs=3", "max_repairs = 3", "default of 3", "default 5"),
    ),
    AnalysisQ(
        id="durable_methods",
        prompt=("List the public methods of the DurableState class in harness/state.py. "
                "Report their exact names."),
        must_contain=("list_jobs", "job_artifacts", "events_since"),
        must_not_contain=("def commit", "def get", "def set", ".save(", "def load"),
    ),
    AnalysisQ(
        id="registry_return",
        prompt=("What does the build() function in pmharness/registry.py RETURN? "
                "State the return type/object."),
        must_contain=("driver",),
        must_not_contain=("dictionary", "returns a dict", "mapping of", "list of"),
    ),
    AnalysisQ(
        id="edit_adapters",
        prompt=("Which adapters does pmharness mark as edit-capable in workers.py "
                "(_EDIT_CAPABLE_ADAPTERS)? Name them exactly."),
        must_contain=("claude-code", "codex"),
        must_not_contain=("openai", "cursor", "local"),
    ),
    AnalysisQ(
        id="pilot_envelope",
        prompt=("What are the two top-level keys of the pilot envelope the pilot model "
                "must emit (harness/pilot.py)? Name them."),
        must_contain=("say", "actions"),
        must_not_contain=("intent", "thought", "tool_name"),
    ),
    AnalysisQ(
        id="pilot_steps_cap",
        prompt=("What is the safety cap on pilot<->swarm round-trips per user message "
                "in harness/conversation.py (_HARD_PILOT_STEPS_DEFAULT)? State the number."),
        must_contain=("40",),
        must_not_contain=("HARD_PILOT_STEPS = 5", "cap of 5", "cap of 8", "10"),
    ),
    AnalysisQ(
        id="provider_count",
        prompt=("How many provider profiles are declared in harness/providers.py "
                "(the PROVIDERS tuple)? State the count and name a few."),
        must_contain=("9", "nine"),
        must_not_contain=("5 provider", "six provider", "12 provider"),
    ),
)


def score_analysis(q: AnalysisQ, finding_text: str) -> dict:
    t = (finding_text or "").lower()
    hit = any(s.lower() in t for s in q.must_contain)
    fab = any(s.lower() in t for s in q.must_not_contain) if q.must_not_contain else False
    if hit and not fab:
        score = 1.0
    elif hit and fab:
        score = 0.5
    elif fab:
        score = 0.0
    else:
        score = 0.25  # silent: didn't find it but didn't fabricate either
    return {"id": q.id, "hit": hit, "fab": fab, "score": score}
