# Compaction Residual Lab

Whitepaper extension for the Layer-0 compaction-residual experiment. The
factory residual is now `catalog` (v0.9.244). This note is the lab history
that justified that flip, not a second product contract.

## Status and question

The factory residual is now `catalog`: an extractive handle index plus a
last-N selected story, with vault retrieve for later lexical asks. Summary
and hybrid remain Settings opt-ins. `off` is still env-only and is never
inferred. This note keeps the lab history that led to that flip.

Research question: can a bounded deterministic handle catalog preserve buried
facts at lower residual cost than the existing causal summary, with
archive-backed `peek_history` as a fallback?

## Implemented seams

`HARNESS_COMPACTION_RESIDUAL` selects how `_maybe_compact_history` rewrites the
compacted middle (`harness/compaction_residual.py`):

| Value | Role |
| --- | --- |
| `summary` | Paid LLM snapshot. Settings opt-in. |
| `catalog` | Factory default. Empty, missing, or unknown values also fall back here — never to `off`. Extractive handle index plus a last-N selected story. |
| `hybrid` | Real LLM four-heading summary plus a capped unique-handle index. Timeout / degenerate / insufficient-reduction fall back to the extractive snapshot plus handles. |
| `off` | Explicit no-compaction test ceiling. Must be set; it is never inferred. |

Catalog and hybrid are extractive and bounded. They collect unique files, tool
names, durable URIs (`spill://`, `artifact://`, `job://`), and
error/decision/constraint stems. Hybrid keeps the four causal headings
(`## Historical Task Snapshot`, `## Resolved`, `## Pending / Open Questions`,
`## Key Facts / Decisions / Files`) and appends `## Unique handles` with
files, tools, URIs, and stems. Neither path invents turn IDs or peek
offsets. Copied residual text is redacted for likely secrets.

Wave 1 archive sidecar (`harness/compaction_archive.py`):

- Path: `{state_dir}/transcripts/{session}.archive.json` (session-scoped;
  same containment as `save_transcript`).
- Writes are atomic UTF-8 JSON. Residual transcript persist does not replace
  this file.
- `peek_history` composes archive + residual. When an archive exists, the
  residual is the live post-compact tail first so a stale pre-compact
  transcript is not concatenated on top of elided rows. Callers may pass
  `expected_generation`; a mismatch returns `stale_generation`.
- Corrupt, foreign-version, or oversized sidecars fail closed (empty archive,
  no raise). Load refuses files above 512 KiB.
- Retention keeps oldest + newest rows under 400 messages / 256 KiB
  serialized bytes. When rows must drop, a single truncation marker is
  inserted; prior markers are stripped so repeated Compact Now cannot grow
  the sidecar without limit.
- Session deletion (`remove_session_transcript`) removes the archive beside
  the transcript.

## Method

The labeled battery lives in `pmharness/compaction_residual_battery.py`. Seven
deterministic cases bury a fact in a long-session middle, then probe whether
the residual (and, for arm C, archive-backed peek) still carries it:

1. **Early constraint** — write-path constraint (`never write to production.db`;
   `scratch.sqlite`) placed before filler turns.
2. **Mid-session file path** — `src/billing/ledger_v3.py` read via a tool call
   with `_read_path`.
3. **Reversed decision** — later `Decision: use SQLite instead of Redis`
   must survive the earlier Redis candidate.
4. **Error-tail fact** — `ERROR: ... secret-policy.yaml ... E-7721` in a
   tool result.
5. **Spill / artifact handles** — `spill://`, `artifact://`, and `job://`
   URIs retained as durable handles.
6. **Distractor twin** — active `auth_current_v2.py` versus retired
   `auth_legacy_v1.py`. The retired name is a false-recall guard, not a
   fabrication token.
7. **Catalog-miss plain fact** — measurement tokens
   (`omega-cache-token-9f3a`, `shard-omega-p95`) with no path or URI shape.
   After fact harvest those tokens are residual facts (`catalog_recalls_fact=True`).
   The Layer-0 table below is the pre-harvest snapshot; do not treat it as
   current catalog behavior.

Arms:

| Arm | Residual | Compaction | Peek |
| --- | --- | --- | --- |
| A | `summary` | yes | no — scripted-summary omission control |
| B | `catalog` | yes | no |
| C | `catalog` | yes | yes — real archive-backed `peek_history` |
| D | `off` | no | no — no-compaction ceiling |

The runner (`pmharness/compaction_residual_bench.py`) drives
`ConversationalSession._maybe_compact_history(force=True)`. It does not use
the single-shot episode rig (`pmharness.run_episode` does not compact
conversations) or `CassetteDriver` (its request hash includes the residual).

Scoring is stdlib substring oracles: `must_contain` is all-of, not any-of;
`must_not_contain` is a false-recall guard. Arm C splits residual vs peek so
a peek-only recovery is credited without blending the two channels into one
blob. Receipts record residual tokens, peek calls, and peek tokens. Default
execution uses no API keys.

## Results

Full battery, this workspace: 28 receipts, 19 successful end-task receipts.

| Arm | End-task success | Residual recall | Notes | Avg residual tokens | Peek |
| --- | --- | --- | --- | --- | --- |
| A | 0/7 | 0/7 | Intentional scripted omission control | 169.0 | 0 calls |
| B | 6/7 | 6/7 | Misses the plain-fact case | 195.0 | 0 calls |
| C | 7/7 | 6/7 | One peek-only recovery | 195.0 | 2.0 avg calls, 808.3 avg peek tokens |
| D | 6/7 | 7/7 raw | 1/7 false recall | 837.9 | 0 calls |

The one C lift is `catalog_miss_plain_fact`: the catalog residual does not
carry the measurement tokens; archive-backed peek recovers them. D's false
recall is `distractor_twin` — the no-compaction ceiling still contains the
deliberately retained retired filename `auth_legacy_v1.py`. Catalog/hybrid
arms that extract unique handles can keep the active file without treating
the retired twin as a required residual fact.

Catalog residual cost (195.0) is far below the uncompacted ceiling (837.9)
and only modestly above the scripted four-heading control (169.0). That
comparison is valid only against this fixture, not against a production
summarizer.

## Interpretation and gate

The catalog is a credible candidate for a bounded hybrid experiment. It
preserved handle-shaped buried facts at a residual much smaller than the
raw transcript, and archive-backed peek recovered the one designed catalog
miss.

This fixture-level result does not justify changing the production default.
Keep `summary` as the default.

Before a go decision, require:

- provider-backed replay with real summarizer outputs (not the scripted
  omission control);
- larger and adversarial batteries;
- task-level probes that decide *when* to call peek, rather than scripting
  the calls;
- cost and latency accounting at the pilot-turn level.

Provisional gate: do not ship catalog-only. Continue hybrid/peek research if
the next battery preserves recall while keeping residual and retrieval
overhead within a predeclared budget.

## Limitations

- Arm A is not a production summarizer benchmark. The mock summary is a
  valid four-heading seed that omits buried tokens on purpose.
- Arm C directly scripts `peek_history` windows. It measures archive
  recoverability, not pilot tool-choice quality.
- The battery is small (seven pattern-based cases) and uses substring
  oracles, not an LLM-as-judge.
- The catalog intentionally does not resolve arbitrary prose and does not
  fabricate turn IDs. Plain facts without a handle shape are expected misses.
- Bounded archive truncation is surfaced with an explicit marker. The
  sidecar does not silently pretend full recall after repeated Compact Now.
- The distractor oracle measures retention more than ranking. For bare
  filenames, keeping both twins in an uncompacted history is a false-recall
  against the retired name, not a claim about which file a model would cite.

## Layer-1 live gates

Layer-1 is a provider-backed protocol on the same research question. It does
not claim that catalog plus retrieval beats LLM compaction. That protocol
froze factory residual at `summary`. The later default-worthy gate (below)
is what flipped empty/invalid `HARNESS_COMPACTION_RESIDUAL` to `catalog`.
`off` is still never inferred.

The earlier paid Sol 7x4x3 core-only run was saturated (rescored 84/84
end-task successes) and is retired as a ranking source. The claim-grade
protocol below uses `--suite all` (10 cases), frozen `end_task/v2`,
`residual_recall_round1` as the primary metric, and two models.

Catalog washout is closed in `extract_handle_index`: catalog and hybrid
bullets are re-extracted so a second compaction does not drop handles that
only survived as residual bullets. Hybrid now also emits a `stems` line
in `## Unique handles` (closed-loop re-ingest, 800-char appendix cap).
That product change landed after the Sol suite-all run; Sol/Luna arm B
in the receipts below is hybrid *without* stems.

The live scorer is frozen as `end_task/v2`. Receipts persist
`residual_text`, `residual_recall_round1`, and `final_answer`. Do not add
a winner field.

Live arm letters (distinct from the Layer-0 hermetic table above):

| Arm | Residual |
| --- | --- |
| A | summary + archive |
| B | hybrid + archive |
| C | catalog + archive |
| D | off / uncompacted ceiling |

Holdout cases stay outside `RESIDUAL_CASES` so Layer-0 remains seven
cases. `live_cases()` is the core seven plus
`negative_control_absent_token`, `long_horizon_early_constraint`,
`distractor_plus_absent_twin`, and `nonce_write_constraint`. `--suite`
selects `core` (default), `holdout`, or `all`. `--case` still overrides.

`nonce_write_constraint` is the harder end-task holdout: allowed write
target `scratch-omega-9f3a.sqlite`, forbidden `prod-omega-lock.db`. The
generic `production.db` / `scratch.sqlite` pair is mentioned as a
convention to ignore so world knowledge cannot pass. Naming only that
generic pair fails the oracle. Luna and Sol A/B/C all kept both nonce
filenames in the first residual and named them in the end-task (3/3
each, ≈ $0.10). Distinctive identifiers survive the summarizer; generic
policy stems (`never write to production.db`) do not. Hybrid+stems is
still the fix for the generic-policy miss.

Pre-registered gates (`evaluate_live_gates`, attached as `gates`):

- `primary_metric` is `residual_recall_round1` (not final residual
  recall, not end-task success). Arm D has no compact rounds, so its
  `residual_recall_round1` is always 0; rank D on `residual_recall`.
- `saturation_fail` if summary and hybrid (A and B, each n≥6) both have
  end-task ≥ 0.95, or if A/B/C all have `residual_recall_round1` ≥ 0.95.
  Catalog+peek end-task ceiling is expected and is not saturation.
- `stale_tax_fail` if arm C has `peek_calls` > 0 and
  `peek_stale / peek_calls` > 0.25.
- `honesty_fail` if a `negative_control` or
  `distractor_plus_absent_twin` row invents the absent token
  (`false_recall`).
- `suite_incomplete_fail` if any `live_cases()` id is missing, or if
  fewer than two models each have a full factorial (every case × every
  arm × 3 repeats).
- `claim_ready` is true only when none of those fails fire, at least two
  distinct models are present, every ok row has `residual_text` and
  `final_answer`, and no `winner` key exists.

Do not declare a winner if `gates.claim_ready` is false.

## Layer-1 Sol suite-all (claim-grade, one model)

Artifact: `artifacts/compaction_residual_live_all_sol.json`.
Driver `openrouter:openai/gpt-5.6-sol`. 10 cases × 4 arms × 3 repeats =
120 ok rows. Provider cost ≈ $1.01. Schema `compaction_residual_live/v2`.
No `winner` keys. Honesty holdout: 12/12 negative-control rows refused
without fabricating a value (`honesty_fail=false`).

Gates on this file: `claim_ready=false` (one model, `saturation_fail`,
`stale_tax_fail`). End-task is near ceiling (A 27/30, B 28/30, C 30/30,
D 30/30) — that is why the primary metric is first-compact residual
recall, not end-task.

| Arm | n | residual_recall_round1 | residual_recall | end-task |
| --- | --- | --- | --- | --- |
| A summary | 30 | 18 | 18 | 27 |
| B hybrid (no stems) | 30 | 18 | 18 | 28 |
| C catalog | 30 | 24 | 24 | 30 |
| D off | 30 | 0 (no compact) | 27 | 30 |

Complementarity (3/3 vs 0/3 on `residual_recall_round1`):

- Catalog keeps constraint/decision stems that the summarizer drops:
  `early_constraint`, `long_horizon_early_constraint`,
  `reversed_decision` are C 3/3 and A/B 0/3.
- The summarizer keeps designed catalog-miss nonces
  (`omega-cache-token-9f3a`, `shard-omega-p95`): A/B 3/3 residual,
  C 0/3 residual. C recovered the end-task via peek (3/3).
- Handle-shaped cases (`mid_session_file_path`, `error_tail_fact`,
  `spill_artifact_handle`, both distractors) are A/B/C 3/3.

Arm C peek tax: 16 calls, 11 success, 5 stale (0.312). Every stale
call is the first peek after compact (`expected_generation` guess),
then a retry succeeds. That is a product tax, not residual loss.

This is not a go decision and not a claim that catalog beats summary.
It is evidence that neither channel alone is sufficient, which is why
hybrid now carries stems next to the LLM snapshot.

## Layer-1 Luna suite-all (second model)

Artifact: `artifacts/compaction_residual_live_all_luna.json`.
Driver `openrouter:openai/gpt-5.6-luna`. Same 10×4×3 protocol. 120 ok
rows. Provider cost ≈ $0.10. Honesty holdout 12/12 clean.

| Arm | n | residual_recall_round1 | residual_recall | end-task |
| --- | --- | --- | --- | --- |
| A summary | 30 | 17 | 17 | 24 |
| B hybrid (no stems) | 30 | 18 | 18 | 23 |
| C catalog | 30 | 24 | 24 | 29 |
| D off | 30 | 0 (no compact) | 27 | 24 |

Luna replicates the Sol complementarity exactly: constraint/decision
cases are C 3/3 and A/B 0/3; catalog-miss nonces are A/B 3/3 and C 0/3.
Handle-shaped cases stay 3/3 on A/B/C. C peek tax 19/13/6 (stale 0.316).
One B `early_constraint` row fabricated (`false_recall` 1/3); negative
control stayed clean.

Luna D end-task dropped on `catalog_miss_plain_fact` (0/3) and
`reversed_decision` (0/3) even with the fact in the uncompacted
transcript. That is a probe/model issue, not a residual ranking; D
`residual_recall` remains 27/30.

## Layer-1 merged Sol+Luna

Artifact: `artifacts/compaction_residual_live_all_sol_luna.json`.
240 ok rows, two models, no `winner` keys, 24/24 negative-control
honest. Combined cost ≈ $1.10.

| Arm | n | residual_recall_round1 | residual_recall | end-task |
| --- | --- | --- | --- | --- |
| A summary | 60 | 35 | 35 | 51 |
| B hybrid (no stems) | 60 | 36 | 36 | 51 |
| C catalog | 60 | 48 | 48 | 59 |
| D off | 60 | 0 (no compact) | 54 | 54 |

Constraint/decision complementarity is 6/6 vs 0/6 on both models:
`early_constraint`, `long_horizon_early_constraint`,
`reversed_decision`. Catalog-miss is the inverse: A/B 6/6, C 0/6
residual (C end-task 5/6 via peek).

Gates: `claim_ready=false`. Two models and honesty pass; saturation
fails (C end-task 59/60) and stale tax fails (11/35 = 0.314). Do not
flip the production default.

## Hybrid+stems confirmation (Luna, complementarity slice)

After adding stems to the hybrid appendix and advertising
`compaction_generation` on the injected residual (with
`expected_generation=0` treated as unset), a focused 4×3×3 Luna rerun
on the complementary cases
(`artifacts/compaction_residual_live_hybrid_stems_luna.json`, ≈ $0.03):

| Arm | residual_recall_round1 | Notes |
| --- | --- | --- |
| A summary | 3/12 | Only `catalog_miss_plain_fact` (3/3). Constraints and reversed decision 0/3. |
| B hybrid+stems | 12/12 | Constraints 3/3, long-horizon 3/3, reversed decision 3/3, catalog-miss 3/3. |
| C catalog | 9/12 | Constraints and reversed decision 3/3; designed catalog-miss 0/3. |

C peek stale rate on this rerun was 0/5 (`stale_tax_fail=false`).

Sol replica (`artifacts/compaction_residual_live_hybrid_stems_sol.json`,
≈ $0.34) is the same pattern: A 3/12, B 12/12, C 9/12
`residual_recall_round1`. Merged confirmation
(`artifacts/compaction_residual_live_hybrid_stems_sol_luna.json`):

| Arm | n | residual_recall_round1 | end-task | C peek stale |
| --- | --- | --- | --- | --- |
| A summary | 24 | 6 (catalog-miss only) | 13 | — |
| B hybrid+stems | 24 | 24 | 20 | — |
| C catalog | 24 | 18 (misses catalog-miss) | 24 | 0/12 |

`claim_ready` stays false on this slice (`suite_incomplete_fail`).
Production default stays `summary`. Hybrid+stems is the candidate
residual: it is the only arm that keeps both constraint stems and
designed catalog-miss nonces after the first compact.

## Layer-1 claim-grade Luna (hybrid+stems, 11x4x3)

Artifact: `artifacts/compaction_residual_live_claim_luna.json`.
Driver `openrouter:openai/gpt-5.6-luna`. 11 cases × 4 arms × 3 repeats
= 132 ok rows. Cost ≈ $0.11. C peek stale 0/13. Honesty 12/12.
`claim_ready=false` (one model). `saturation_fail=false`.

Fact-bearing residual_recall_round1 (excludes negative control):

| Arm | residual_recall_round1 | Misses |
| --- | --- | --- |
| A summary | 21/30 | `early_constraint`, `long_horizon_early_constraint`, `reversed_decision` (0/3 each) |
| B hybrid+stems | 30/30 | none |
| C catalog | 27/30 | `catalog_miss_plain_fact` (0/3) |

B is the only compact residual that keeps both channels.

## Layer-1 claim-grade merged Sol+Luna

Artifacts: `artifacts/compaction_residual_live_claim_sol.json` (≈ $1.13)
and `artifacts/compaction_residual_live_claim_luna.json` (≈ $0.11),
merged at `artifacts/compaction_residual_live_claim_sol_luna.json`.
11 cases × 4 arms × 3 repeats × 2 models = 264 ok rows. No `winner`
keys. C peek stale 0/24. Honesty 24/24 (negative control plus absent
twin). `evaluate_live_gates` on the merge: `claim_ready=true`.

Fact-bearing `residual_recall_round1` (excludes negative control):

| Arm | n | residual_recall_round1 | Misses |
| --- | --- | --- | --- |
| A summary | 60 | 42 | all 18 generic-policy cells (`early_constraint`, `long_horizon_early_constraint`, `reversed_decision`) |
| B hybrid+stems | 60 | 60 | none |
| C catalog | 60 | 54 | all 6 `catalog_miss_plain_fact` cells |
| D off | 60 | 0 (no compact) | rank D on `residual_recall` (60/60) |

The two models replicate the same split. Sol B end-task is 33/33; Luna
B residual is still 30/30 fact-bearing. Distinctive identifiers
(`nonce_write_constraint`, handles, error tails) survive summary;
generic policy stems do not. Catalog keeps stems and drops nonce
prose. Hybrid+stems is the only compact residual that keeps both.

This is a residual-ranking claim, not a default flip. Production
`HARNESS_COMPACTION_RESIDUAL` stays `summary` until a product cut
opts into hybrid. Do not write a `winner` field onto receipts.

## Reproduction

Hermetic battery (no API keys):

```
./.venv/bin/python -m pmharness.compaction_residual_bench
```

Layer-1 live protocol (default dry-run, no network):

```
./.venv/bin/python -m pmharness.compaction_residual_live
```

Live provider replay requires explicit `--live` and `--driver`. `--suite`
defaults to `core` (the Layer-0 seven). Use `--suite holdout` or
`--suite all` for Layer-1 holdouts. Live arms are A=summary+archive,
B=hybrid+archive, C=catalog+archive, D=off. The runner never scripts
`peek_history`; the pilot decides retrieval.

Focused compaction / residual / archive / peek suite:

```
./.venv/bin/python -m pytest -q tests/test_compaction_residual.py tests/test_compaction_residual_bench.py tests/test_compaction_archive.py tests/test_peek_tools.py tests/test_compaction.py tests/test_compaction_quality_guards.py tests/test_compaction_mixin.py tests/test_compaction_timeout.py
```

Full local suite used for this note:

```
./.venv/bin/python -m pytest -q
```

That full run reported 4450 passed, 74 skipped locally.

## Fact harvest and obligation stems (post claim-grade)

Do not rewrite the 264-row claim-grade table. That receipt is a prior
residual generation. After it landed, two deterministic harvests were
added to `extract_handle_index`:

- **Facts** — hyphen/underscore identifiers with a digit and at least two
  separators (`omega-cache-token-9f3a`). File/handle/tool tokens are
  excluded. Catalog emits `### Facts`; hybrid emits `- facts:`.
- **Unprefixed obligation lines** — raw transcript lines containing
  `never ` / `must not ` / `do not ` / `don't ` / `instead of ` /
  `rather than ` / `the only `. Structured catalog/hybrid residuals are
  not re-harvested this way (closed-loop ingest already classifies stems).

Hermetic Layer-0 after those harvests: catalog residual recall on
`catalog_miss_plain_fact` is now true (B and C), so the designed
catalog-miss hole is closed at fixture level. `unprefixed_obligation` is
opt-in (`EXPERIMENTAL_CASES`); it is not in `live_cases()` and cannot
change `claim_ready`.

Luna complementarity slice (36 ok trials, ~$0.034,
`artifacts/compaction_residual_live_fact_harvest_luna.json`):

| Arm | residual_recall_round1 | end-task |
| --- | --- | --- |
| A summary | 6/12 | 7/12 |
| B hybrid | 12/12 | 10/12 |
| C catalog | 12/12 | 12/12 |

A still drops generic `CONSTRAINT:` / `Decision:` policy
(`early_constraint`, `reversed_decision`). Catalog-only matched hybrid on
residual recall and beat it on end-task for this cheap model. That is a
replacement *candidate* on this slice, not a default flip.

Follow-on harvests now in the working tree: last-wins stems, version/ticket
facts, polarity phrases (`go ahead` / `is retired`), Unicode apostrophe
fold, and a file-stem fact filter so `auth_legacy_v1` does not leak.
Experimental cases `version_pin`, `unprefixed_reversal`, and
`stem_cap_later_decision` stay opt-in.

Sol slice (63 ok trials, ~$0.59,
`artifacts/compaction_residual_live_fact_harvest_sol.json`):

| Arm | residual_recall_round1 | end-task |
| --- | --- | --- |
| A summary | 11/21 | 12/21 |
| B hybrid | 18/21 | 18/21 |
| C catalog | 21/21 | 21/21 |

Catalog-only is the only compact residual that kept every buried token
on this slice, including last-wins after twelve filler obligations and
an unprefixed reversal. Hybrid dropped `stem_cap_later_decision` when
the appendix sliced the oldest six stems; that appendix now takes the
newest six. Luna stem-cap confirm after that fix
(`artifacts/compaction_residual_live_stem_cap_luna.json`): hybrid B 3/3
round-1 residual, catalog C 3/3 residual and end-task. Summary still
drops generic policy and the unprefixed reversal. Do not flip the
production default.

## Vault retrieve (new angle)

The 256KB JSON peek sidecar cannot hold a 1M-token dump. Compact now
also indexes the elided middle into `compaction_vault.sqlite` (FTS5,
session-scoped, 4000 chunks / 8MB). Later user asks retrieve matching
slices and inject them beside wiki grounding. This does not write or
query the wiki.

Science claim to test: catalog residual can miss plain prose; vault
retrieve should still return it. Experimental case
`vault_only_prose_cutoff` (`fourteenth of each month`) is opt-in
`--case`, not in `live_cases()` / `claim_ready`. Live receipts now
record `vault_hits` / `vault_recall` / `vault_false_recall` separately
from residual recall. Factory default stays `summary`.

Luna n=3/arm, not claim_ready
(`artifacts/compaction_residual_live_vault_angle_luna.json`,
rescored after the end-task oracle accepted `14th` as well as
`fourteenth`):

| Condition | residual | vault | peek | end-task |
| --- | --- | --- | --- | --- |
| A summary + vault | 1/3 (r1 2/3) | 3/3 | 0 | 3/3 |
| C catalog + vault | 0/3 | 3/3 | 0 | 3/3 |
| C catalog, vault off | 0/3 | 0/3 | 2 | 2/3 (peek) / 1 refuse |
| negative control A/C | 0/3 | 0/3 | 0 | 3/3 refuse, no invent |

Vault-off still recovered via `peek_history` because the archive keeps
oldest+newest and this case is tiny. After `hide_peek=True`, Luna
catalog C (n=3, not claim_ready):

| Condition | residual | vault | peek | end-task |
| --- | --- | --- | --- | --- |
| vault on, peek hidden | 0/3 | 3/3 | 0 | 3/3 |
| vault off, peek hidden | 0/3 | 0/3 | 0 | 0/3 refuse |

That is the unique dump-and-query lift: catalog residual empty, no
peek, only the SQLite inject answers. Hermetic
`test_vault_survives_peek_archive_middle_eviction` shows the 256KB
sidecar drops a mid-history cutoff while the vault still returns it.

Two-model validation (n=3/cell, still not `claim_ready`; paper draft
`artifacts/compaction_vault_whitepaper.md`):

| Cell | Luna end-task | Sol end-task |
| --- | --- | --- |
| peek hidden, vault on | 3/3 | 3/3 |
| peek hidden, vault off | 0/3 refuse | 0/3 refuse |
| 85k eviction, vault on | 3/3 (0 peek) | 3/3 (0 peek) |
| 85k eviction, vault off | 0/3, peek miss | 0/3, peek miss |

Eviction compact: 84904 → 269 tokens, extractive, $0 summarizer.
Vault-off peek does not recover the mid-history sentence.

Default-gate cells (narrative recap, no-overlap paraphrase, false twin):

| Cell | Luna A summary | Luna C catalog | Sol A | Sol C |
| --- | --- | --- | --- | --- |
| narrative recap | 3/3 | 0/3 | 3/3 | 0/3 |
| paraphrase no-overlap | 3/3 | 0/3 | 3/3 | 0/3 |
| false-retrieve twin | 3/3 (vault also injects retired) | 3/3 | (not run) | (not run) |

Catalog+vault failed because the ask was not a key into the dump.
The buried line was indexed. Recap FTS searched `earlier`/`decided`
and retrieved the tail (`earlier facts`). Paraphrase FTS searched
`invoices`/`freeze` and retrieved nothing. The model trusted the
wrong inject or invented billing trivia. Summary still works because
it *selects* at compact time.

The missing piece is compact-time selection plus anaphora/miss
routing: write a recap-addressable plan chunk (non-filler user
lines + "what we decided"), use it on recap asks or empty FTS.
Luna catalog C after that change (`*_vault_recap_route_luna.json`):
narrative 3/3 `recap_plan`, paraphrase 3/3 `miss_plan`, lexical
cutoff still `fts` 3/3, twin still injects both regions. Inject on
recap dropped from ~1.6k junk chars to ~466. Factory default stays
`summary`; the selector heuristic is still lab-shaped. Paper:
`artifacts/compaction_vault_whitepaper.md`.

## Selector failure inventory (not claim_ready)

The recap/miss 12/12 was fitted to lab filler (`docs only` + `pad pad
pad`). Adversarial cases live in `EXPERIMENTAL_CASES` only. Luna A
versus C, n=3, peek hidden, $0.032
(`artifacts/compaction_residual_live_vault_selector_fail_luna.json`,
rescored miss-oracle in `*_rescored.json`).

| Cell | A summary | C catalog+vault | What failed |
| --- | --- | --- | --- |
| plausible chrome leak | 3/3 | 3/3 | Plan keeps canary *and* changelog/CI notes. Leak is visible; fact survives. |
| real plan contains `docs only` | 3/3 | 0/3 | `_filler_like` drops the decision. Recap injects only the tail docs pass (355 chars). Answers: "continue the current documentation pass." |
| first-12 cap / late spare | 1/3* | 3/3** | Hermetic plan keeps retired primary, drops spare. Live C 3/3 is not last-wins: compact residuals are `role=user`, so the next plan chunk harvested gen-1 catalog `Last ask` (spare). |
| assistant-only decision | 3/3 | 0/3 | Plan keeps the question, not "Ship the canary to the spare region." Model honestly says no decision is recorded. |
| empty-FTS wrong plan | 0/3 | 2/3*** | `miss_plan` injects the canary into "When do invoices freeze?" Arm A always mentions spare. C: 2 honest refuses, 1 contamination. |
| bare `remind` false-fire | 3/3 | 3/3 | Route is `recap_plan` and vault_false_recall 3/3, but Luna followed the new instruction and did not leak spare. |

\* A cap 1/3 is partly oracle-harsh: two "failures" correctly said
spare replaced primary and were dinged for naming `primary region`.
\*\* Confounded on the first Luna run. After `build_plan_recap_chunk`
skips `_compressed_summary` / `[Earlier conversation summarized`,
Luna C cap is 0/3 and answers **primary region**
(`*_vault_selector_after_skip_luna.json`, $0.0035). Narrative and
paraphrase stayed 3/3. Docs-only and assistant-only stayed 0/3.
\*\*\* Rescored after the miss oracle accepted "I don't have … in the
available context." Raw C miss was 0/3.

Strongest product blockers, ranked:

1. **miss_plan** — any empty FTS dumps the compact-time user-line
   extract. Helps a paraphrase that happens to be in the plan; poisons
   an unrelated ask. Even summary+vault contaminates (A 0/3).
2. **user-only extract** — assistant/tool decisions vanish. Typical
   session shape is a short user question plus an assistant plan.
3. **`docs only` substring** — a real constraint that uses those words
   is dropped; lab filler is exactly those words.
4. **Residual-as-user re-harvest** — later compacts treat the injected
   residual as a plan line. `_PLAN_MATCH` is untyped English
   (`Earlier AND decisions AND plans`), so catalog/summary blobs become
   `recap_plan` hits. This is why C cap looked like a selector win.

Additional mechanisms from `job_9a40be361bbc` (code-true, not live-run):
recap preempts lexical FTS on mixed asks; plan blob lives in the same
FTS table; plan snapshots are append-only BM25 top-2, not latest;
2000-char first-wins clip; recap regex is both too narrow (`what did we
agree`) and too wide (`from earlier`, `opening plan`); unique-word
filler drops repetitive real constraints; `vault_match_query` drops
tokens shorter than 3.

That inventory is why catalog was not factory default yet. The cut below
is what closed it.

## Default-worthy gate (pre-registered, not claim_ready)

Catalog cannot be factory default while it is only an index. Vault is
lookup. A later ask that shares no tokens with the buried line will
miss unless the live residual already contains a selected story.
`miss_plan` is not that story: it helps a lab paraphrase and poisons
an unrelated empty-FTS ask.

The product cut that can close the gap: put an extractive last-N
selected story on the catalog residual at compact time, harvest user
and assistant lines, skip injected residuals, keep `docs only` when
the line has distinctive content, and leave empty FTS empty. Recap
asks still retrieve the plan chunk. Factory default stays `summary`
until both models pass the gate below.

Pre-registered (write this before the live receipt exists):

- Drivers: Luna and Sol (`openrouter:openai/gpt-5.6-luna`,
  `openrouter:openai/gpt-5.6-sol`).
- Arms: A summary vs C catalog+vault. n=3. Peek hidden except
  `vault_peek_evicted_cutoff`.
- Cases: narrative, paraphrase, twin, cutoff, peek-evicted, cap
  last-wins, docs-only plan, assistant-only, miss-wrong-plan,
  recap false-fire, plausible filler, plus the honesty negative
  control. Experimental only; do not add these to `live_cases()`.
- Pass: C end-task >= A on narrative, paraphrase, twin, and cutoff.
  Paraphrase C must be >= 2/3 or the gate fails closed.
  Docs-only, assistant-only, and cap last-wins C >= 2/3.
  False-fire: no spare leak on >= 2/3.
  Miss-wrong: do not invent a freeze date; C no worse than A
  (both may mention the canary because the story is now in the
  live residual, same as summary-in-window).
- Honesty negative control must still refuse an absent token.
- No `winner` field. No factory flip if either model misses
  paraphrase. Empty/invalid `HARNESS_COMPACTION_RESIDUAL` maps to
  `catalog` only after both models pass.

## Default-worthy results (Luna + Sol, n=3, not claim_ready)

Receipts: `artifacts/compaction_residual_live_default_worthy_luna.json`
($0.082) and `*_sol.json` ($0.830). 16 cases, A vs C, 96 trials each.

| Cell | Luna A | Luna C | Sol A | Sol C |
| --- | --- | --- | --- | --- |
| narrative recap | 3/3 | 3/3 `recap_plan` | 3/3 | 3/3 |
| paraphrase no-overlap | 3/3 | 3/3 `empty` | 3/3 | 3/3 |
| twin | 3/3 | 3/3 `fts` | 3/3 | 3/3 |
| cutoff prose | 3/3 | 3/3 `fts` | 3/3 | 3/3 |
| peek-evicted cutoff | 3/3 | 3/3 `fts` | 3/3 | 3/3 |
| last-N cap / late spare | 1/3* | 3/3 | 3/3 | 3/3 |
| docs-only real plan | 3/3 | 3/3 | 3/3 | 3/3 |
| assistant-only decision | 3/3 | 3/3 | 3/3 | 3/3 |
| invoice ask / canary plan | 3/3 | 3/3 `empty` | 2/3** | 3/3 |
| remind test runner | 3/3 | 3/3 | 3/3 | 3/3 |
| chrome notes | 3/3 | 3/3 | 3/3 | 3/3 |
| negative control | 3/3 | 3/3 | 3/3 | 3/3 |
| unprefixed reversal | 0/3 | 0/3 | 0/3 | 0/3 |
| stem-cap later decision | 3/3 | 3/3 | 3/3 | 2/3*** |

Paraphrase C is empty-vault: the selected story in the catalog residual
carries `twenty-seven`. Miss-wrong is also empty-vault; the model refuses
a freeze date instead of dumping the canary. That is why `miss_plan` could
die.

\* Luna A cap named `primary region` as replaced (oracle-harsh).
\*\* Sol A miss used “wasn’t specified”; the refusal list wants “not
specified” / “don’t have”.
\*\*\* Sol C said “use SQLite, not Redis” instead of the phrase
“sqlite instead of redis”. Residual recall was 3/3.

Both models pass the pre-registered gate. Factory residual is now
`catalog`. Topic last-wins drops an earlier line that shares content
nouns with a later one (`apply_topic_last_wins` on story, stems, and
vault retrieve in insert order). One-word acks such as `Reversed.`
stay out of the story so they cannot undo the later policy. Luna
catalog C is 3/3 on unprefixed reversal
(`*_last_wins_noreverse_luna_rescored.json`); Sol C is also 3/3
(`*_last_wins_noreverse_sol.json`). Unprefixed-obligation (no
reversal) stays 3/3 on C.

## Summarizer last-wins (Luna + Sol, n=3, not claim_ready)

The remaining hole after catalog last-wins was summary A: the paid
paragraph still saw `Reversed.` in the middle and treated it as a
rollback (Luna 0/3, Sol 1/3). Product cut:

- `_format_block_for_summary` drops ack-only assistant lines.
- Summarizer system prompt: later decisions replace earlier ones;
  one-word acks are not policy.
- After a successful LLM summary, hybrid handle-index, or extractive
  fallback, `append_selected_story` pins `### Selected story` so a
  lying paragraph cannot hide the later go-ahead.

Receipts: `artifacts/compaction_residual_live_summarizer_last_wins_luna.json`
($0.011) and `*_sol.json` ($0.094). A vs C, two cases, 12 trials each.
All compact rounds were real LLM summaries (`mode=llm`).

| Cell | Luna A | Luna C | Sol A | Sol C |
| --- | --- | --- | --- | --- |
| unprefixed reversal | 2/3 | 3/3 | 2/3 | 3/3 |
| unprefixed obligation | 3/3 | 3/3 | 3/3 | 3/3 |

Gate was A reversal ≥ 2/3 with C still 3/3 and obligation unregressed.
Both models meet it. Residual recall is 3/3 on every cell; the A
misses are lexical (`writable` / `approved write sink`, or
`previously the only sink` tripping the old-policy check), not a
rollback to don't-write. Do not loosen the oracle again to force 3/3.
Do not restamp `claim_ready`. Do not add these cells to `live_cases()`.

Do not tweet “stop summarizing” as a universal win. Summary remains a
Settings opt-in for people who want a paid paragraph. The factory
residual stays `catalog`.
