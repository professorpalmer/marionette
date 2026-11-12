# Recorded router evaluation

`bench/router_evaluation.py` compares immutable captures of the same six heldout
analysis tasks. It uses `pmharness.analysis_bench.score_analysis` directly: a job
and every task must be complete, and all labeled facts must match. Good prose,
partial answers and recorded gate self-reports do not establish success. This
measures exact factual completion, not general reasoning quality.

The fixed manifest hashes task definitions, split assignments, label source
files and the scorer. `registry_return` is the training split; all other existing
analysis questions are heldout. These published questions are not novel or
contamination-resistant. The two repair questions are correlated. Freeze the
manifest before configuring policies; do not tune on heldout outcomes. Source
changes require a new manifest and rerunning both policies.

## Design and ownership

Two alternatives were considered: extend the worker-launching
`analysis_bench_runner`, or evaluate captured public records offline. The latter
keeps launches and production routing outside this slice. `manifest(repo)` pins
the task/snapshot identity, `ingest(store, job_id, task_id, snapshot_sha256)`
captures one public job, `run_record(...)` binds policy/configuration/records,
and `compare(...)` performs the paired report. The canonical PM `JobRef` is
reused; there is no second scorer or job-reference model.

## Usage

From this repository, use the installed shipping pin:

```sh
python -I -c 'import sys; sys.path.insert(0,"."); from bench.router_evaluation import main; raise SystemExit(main())' manifest > manifest.json
```

Use the same launcher with `ingest manifest.json STATE_DIRECTORY JOB_ID TASK_ID`
to capture a previously recorded job. It reads public SQLite store APIs and does
not launch a worker or reprice the receipt. Store opening follows PM's normal
store initialization behavior; use an offline copy if the original must remain
byte-for-byte untouched. The two reads detect ordinary concurrent changes but
are not a transactional snapshot.

Build each policy's JSON envelope in Python:

```python
import json
from pathlib import Path
from bench.router_evaluation import run_record
snapshot = json.loads(Path('manifest.json').read_text())
records = [json.loads(path.read_text()) for path in Path('captures').glob('*.json')]
run = run_record('baseline', {'policy_revision': 'EXACT_REVISION',
    'routing_config': {}, 'model_versions': {}, 'seed': 0},
    snapshot, records, origin='recorded')
Path('baseline.json').write_text(json.dumps(run))
```

Then use the launcher with `compare manifest.json baseline.json candidate.json`.
Malformed input produces an INCONCLUSIVE JSON error and exit code 2; a valid
but inconclusive report exits 0. Hashes detect accidental modification and bind
identities; they are not signatures and cannot authenticate dishonest inputs.
The collector must independently attest that each job actually used the named
policy, complete configuration, exact prompt and source snapshot. Capture files
can contain task text and artifact contents; keep them local.

## Accounting and prerequisites

Every heldout task remains in the denominator. Missing tasks count as failures
and are also reported separately. Unequal coverage yields zero compared pairs,
not an intersection with missing tasks silently discarded. Per-task exact
scorer outcomes, completion outcomes, abstentions, unknown costs and sample
counts are retained. Duplicate tasks and reuse of a job within or across policies fail.

PM 1.22.48 `select_usage_records` selects one usage record per task, including
when retries occurred. Its frozen terminal receipt therefore cannot prove
all-attempt spend. The report preserves selected marginal cost, raw frozen
receipt, nominal usage pricing and token usage separately. Unknown prices stay
unknown; an explicitly priced subscription zero stays zero selected marginal
cost. Neither zero nor nominal pricing is converted into claimed savings.
All-attempt cost and cost per verified completion remain null, including when
there are no successes. No savings or routing-quality improvement is claimed.

End-to-end latency defaults to unknown. A recording harness may populate
`latency_ms` before resealing the record, measured from dispatch through all
retries and final artifact persistence with a monotonic clock. P95 uses nearest
rank only when every heldout task has valid latency. Partial samples expose
counts but no tail statistic.

A live quality/cost evaluation requires distinct real policy runs on every same
heldout task, pinned complete configs/models/source/scorer, exact raw claims,
terminal outcomes, independently captured latency, and a complete priced ledger
of every attempt (including failed retries). That ledger needs a future explicit
adapter; this version deliberately cannot promote PM selected receipts into
all-attempt totals. Until that evidence exists, verdicts remain INCONCLUSIVE.
Even descriptive success differences in these six tasks are not a statistical
quality-improvement claim.

`tests/test_router_evaluation.py` creates actual temporary public PM SQLite jobs,
tasks and artifacts and round-trips their receipts through the ingestion CLI.
It also calls PM's receipt builder with a supplied empty registry, so no provider,
credential, network request or worker is needed. Fixture outcomes validate the
contract only and provide no model-quality evidence.
