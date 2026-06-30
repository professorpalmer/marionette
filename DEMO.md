# Marionette -- Demo Walkthrough

A 10-minute walkthrough of the PM-native harness: what it is, why it exists, and
how to see it work. Written to be run live.

## The one-sentence version

A coding/agent front-end where Puppetmaster orchestration is the kernel and a
cheap open-weights model is a swappable driver -- so you get the orchestration
without paying a frontier model to narrate it, and (optionally) with no vendor
black box anywhere in the stack.

## Why this exists (the problem it solves)

Every mainstream harness -- Cursor, Claude Code, Hermes -- runs a frontier model
as the top-level narrator. That model pays tokens to *talk about* what it will
do before it does it, and you never control the model: it is a vendor black box.

Two consequences that matter at an enterprise:
1. Cost. The narrator tax is real and recurring.
2. Control. Your prompts and context flow through a model you cannot inspect,
   restrict, or host.

Marionette inverts it: orchestration is the kernel; the model is a component
inside it that emits structured intents, not prose. Swap the model freely. Run
open weights and the black box disappears.

## The honest framing (so nobody oversells it)

- "No black box" has two deployments: self-hosted open weights (no black box,
  data stays in-network, needs GPUs) OR a hosted open-weights provider (cheap,
  but data leaves your network). Same architecture, different trade-off. Say
  which one you mean.
- This is internal-first and deliberately v0.x. It is not Cursor parity and not
  a public product. It is a working proof that the inversion holds.

## The receipt (why a cheap open model is enough)

We built an evaluation that measures the ONE thing the driver must do: emit valid
orchestration intents and make correct decisions (swarm vs answer vs stop) across
multi-turn loops, including read-decide traps where the right move depends on
reading the findings.

Ranked, live, against real Puppetmaster (Stage 4 read-decide traps):

| Rank | Model | Score | tokens | license |
|------|-------|-------|--------|---------|
| 1 | qwen3-coder-30b | 100% | 535 | Apache-2.0 |
| 2 | glm-5.2 | 100% | 974 | MIT |
| 3 | glm-4.7-flash | 100% | 3577 | MIT |
| ... | (competent middle) | 81-90% | | |
| 8 | kimi-k2.6 | 53% | 4809 | other |

A second battery (budget-aware) agrees on the top. The frontier controls
(Claude, Gemini) also score 100% -- i.e. the cheap open winner is
indistinguishable from frontier on the job that matters, at a fraction of the
cost. The losers fail by stopping WITHOUT investigating a task whose answer only
appears after a swarm -- a real, caught failure mode, not a synthetic one.

Default driver: qwen3-coder-30b. Quality + lowest tokens + cleanest license.

## Live demo (run these)

Prereqts: an OpenRouter key in OPENROUTER_API_KEY (or use the no-key stub).

1. Health check -- prove the setup is sound:
   ```
   harness doctor
   ```

2. Headless, trivial task -- the driver does NOT swarm trivia (the cost thesis):
   ```
   harness "What does the acronym REST stand for?"
   ```
   -> answers directly, zero orchestration.

3. Headless, real investigation -- the driver runs Puppetmaster and concludes:
   ```
   harness "Investigate how the durable state layer works here and conclude."
   ```
   -> investigates, narrows, stops with a grounded rationale.

4. The GUI -- the three-pane durable-state view:
   ```
   harness gui
   ```
   Open http://127.0.0.1:8799. Left: driver + session jobs. Center: the driver
   loop live (intent -> Puppetmaster running -> artifacts -> stop). Right: durable
   state (every artifact, inspectable).

5. Reproduce the receipt yourself:
   ```
   harness eval --driver qwen3-coder-30b --reach openrouter --stage s4
   ```

6. Vision, fully open (no frontier model anywhere):
   ```
   HARNESS_VLM_REACH=openrouter harness --image screenshot.png "What is in this image?"
   ```
   An open VLM transcribes the image to text; the open driver reasons over it.

## What to look at in the GUI

- The center pane is the whole pitch: you watch orchestration happen, step by
  step, driven by a model you could self-host. No narration tax, no black box.
- The right pane is durable state -- the artifacts persist in Puppetmaster's
  store across restarts. This is the inspectable DB of everything the system did.

## What it is NOT (yet)

- Not Cursor parity. Not public. Not a model-training pipeline.
- The vision sidecar default is a frontier stand-in unless you set the open VLM
  env; both are one flag apart.
- The eval ranks competent-vs-lazy drivers; splitting the very top tier would
  need harder tasks. The top is stable; we did not overclaim a fine ranking.

## The ask / the conversation

This proves the inversion works and that cheap open weights are sufficient to
drive it. The open questions are deployment (self-host vs hosted provider) and
whether this is worth pointing at a real internal workflow. That is the
discussion to have together.
