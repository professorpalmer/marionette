# Compaction Residual Lab

Whitepaper extension for the completed Layer-0 compaction-residual experiment.
This note records what shipped as an opt-in lab, what the hermetic battery
measured, and what is still required before any production-default change.

## Status and question

This is an opt-in Layer-0 lab, not a production-default recommendation. The
shipping residual remains the existing causal summary. Catalog, hybrid, and
`off` exist so a fixture-level comparison can be run without changing default
behavior.

Research question: can a bounded deterministic handle catalog preserve buried
facts at lower residual cost than the existing causal summary, with
archive-backed `peek_history` as a fallback?

## Implemented seams

`HARNESS_COMPACTION_RESIDUAL` selects how `_maybe_compact_history` rewrites the
compacted middle (`harness/compaction_residual.py`):

| Value | Role |
| --- | --- |
| `summary` | Default. Empty, missing, or unknown values also fall back here — never to `off`. |
| `catalog` | Deterministic unique-handle index extracted from the pruned middle. |
| `hybrid` | Existing extractive four-heading body plus a capped handle index. |
| `off` | Explicit no-compaction test ceiling. Must be set; it is never inferred. |

Catalog and hybrid are extractive and bounded. They collect unique files, tool
names, durable URIs (`spill://`, `artifact://`, `job://`), and
error/decision/constraint stems. Hybrid keeps the four causal headings
(`## Historical Task Snapshot`, `## Resolved`, `## Pending / Open Questions`,
`## Key Facts / Decisions / Files`) and appends `## Unique handles`. Neither
path invents turn IDs or peek offsets. Copied residual text is redacted for
likely secrets.

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
   The catalog is designed to miss this case so arm C can measure peek lift
   separately from residual recall.

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

## Reproduction

Hermetic battery (no API keys):

```
./.venv/bin/python -m pmharness.compaction_residual_bench
```

Focused compaction / residual / archive / peek suite:

```
./.venv/bin/python -m pytest -q tests/test_compaction_residual.py tests/test_compaction_residual_bench.py tests/test_compaction_archive.py tests/test_peek_tools.py tests/test_compaction.py tests/test_compaction_quality_guards.py tests/test_compaction_mixin.py tests/test_compaction_timeout.py
```

Full local suite used for this note:

```
./.venv/bin/python -m pytest -q
```

That full run reported 4450 passed, 74 skipped locally.
