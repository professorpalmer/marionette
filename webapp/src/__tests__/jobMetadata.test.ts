import backend from './jobMetadata.backend.json';
import { describe, expect, it } from 'vitest';
import { advanceMetadataStream, initialMetadataStream, mergeMetadataRows, metadataListPath, metadataSelectionKey,
  metadataStreams, validateMetadataTarget, parseMetadataDetail, parseMetadataList, parseMetadataPins, parseMetadataView } from '../lib/jobMetadata';
import type { MetadataObservation, MetadataStreamState, MetadataView, MetadataRemoval } from '../lib/jobMetadata';
import { context, detail, initial, list, selection, stream, summary, token, view } from './jobMetadata.fixtures';

describe('version 1 boundary', () => {
  it('preserves null counts, missing coverage and unavailable authority', () => {
    const parsed = parseMetadataList(list(), context, stream, initial);
    expect(parsed.rows[0]).toMatchObject({ task_count: null, artifact_count: null });
    expect(parsed.missing).toContain('legacy_ownership');
    expect(parsed.coverage.legacy).toBe('unavailable');
    expect(parseMetadataDetail(detail(), context, selection(), { task_cursor: null, artifact_cursor: null }).cancellation_authority).toBe(false);
  });
  it.each([0, 2, '1', null])('rejects version %s', version => {
    expect(() => parseMetadataList({ ...list(), version }, context, stream, initial)).toThrow();
    expect(() => parseMetadataView({ ...view(), version }, context)).toThrow();
    expect(() => parseMetadataDetail({ ...detail(), version }, context, selection(), { task_cursor: null, artifact_cursor: null })).toThrow();
  });
  it.each(['session_id', 'repo', 'view_generation', 'scope'])('rejects foreign context %s', key => {
    expect(() => parseMetadataList({ ...list(), context: { ...context, [key]: 'foreign' } }, context, stream, initial)).toThrow();
  });
  it('removal contains only ref/revision/deleted and does not leak foreign fields', () => {
    const removed = { selection: selection(), revision: 2, deleted: true };
    const parsed = parseMetadataList({ ...list(), rows: [removed] }, context, stream, initial);
    expect(parsed.rows).toEqual([removed]);
    for (const field of ['ownership', 'lifecycle', 'task_count', 'display']) {
      expect(() => parseMetadataList({ ...list(), rows: [{ ...removed, [field]: 'foreign-secret' }] }, context, stream, initial)).toThrow();
    }
    const foreign = { ...summary(), selection: { ...selection(), source: 'cli' } };
    expect(() => parseMetadataList({ ...list(), rows: [foreign] }, context, stream, initial)).toThrow();
  });
  it('validates UTF-8 selector bytes, preserving long metadata owner values', () => {
    const valid = { ...context, repo: '/' + '界'.repeat(341) };
    expect(parseMetadataView({ ...view(), context: { ...view().context, repo: valid.repo } }, valid).context.repo).toBe(valid.repo);
    const invalid = { ...context, repo: '/' + '界'.repeat(342) };
    expect(() => parseMetadataView({ ...view(), context: { ...view().context, repo: invalid.repo } }, invalid)).toThrow();
    expect(() => parseMetadataView({ ...view(), context: { ...view().context, session_id: 'a'.repeat(257) } }, { ...context, session_id: 'a'.repeat(257) })).toThrow();
    const d = detail();
    d.tasks.rows[0].binding = { task_id: 'task-1', generation: null, lease_id: null, owner: '界'.repeat(1000) };
    expect(parseMetadataDetail(d, context, selection(), { task_cursor: null, artifact_cursor: null }).tasks.rows[0].binding?.owner).toHaveLength(1000);
  });
  it.each([NaN, Infinity, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, '1'])('rejects unsafe numeric revision %s', revision => {
    expect(() => parseMetadataList({ ...list(), page: { ...list().page, revision } }, context, stream, initial)).toThrow();
  });
  it.each(['', 'a', 'x'.repeat(8193), 'a=b=', 'bad cursor', 'a'.repeat(45)])('rejects malformed cursor', cursor => {
    expect(() => metadataListPath(context, stream, { ...initial, cursor })).toThrow();
  });
  it('rejects repeats, missing cursors, false checkpoints and oversized response row counts', () => {
    const partial = { ...list([]), page: { outcome: 'partial', revision: 9, checkpoint: 0, scanned: 51, next_cursor: token() } };
    expect(parseMetadataList(partial, context, stream, initial).rows).toEqual([]);
    expect(() => parseMetadataList(partial, context, stream, { ...initial, cursor: token() })).toThrow();
    expect(() => parseMetadataList({ ...partial, page: { ...partial.page, next_cursor: null } }, context, stream, initial)).toThrow();
    expect(() => parseMetadataList({ ...partial, page: { ...partial.page, checkpoint: 9 } }, context, stream, initial)).toThrow();
    expect(() => parseMetadataList(list(Array.from({ length: 51 }, (_, i) => summary(i))), context, stream, initial)).toThrow();
    expect(() => parseMetadataList(list([summary(), summary()]), context, stream, initial)).toThrow();
  });
  it('pins reject omitted, duplicate, foreign and mismatched rows', () => {
    const result = { version: 1, context, results: [{ selection: selection(), result: { kind: 'present', row: summary() } }] };
    expect(parseMetadataPins(result, context, [selection()]).results).toHaveLength(1);
    expect(() => parseMetadataPins({ ...result, results: [] }, context, [selection()])).toThrow();
    expect(() => parseMetadataPins({ ...result, results: [...result.results, ...result.results] }, context, [selection()])).toThrow();
    expect(() => parseMetadataPins(result, context, [selection(2)])).toThrow();
    expect(() => parseMetadataPins({ ...result, results: [{ ...result.results[0], result: { kind: 'present', row: summary(2) } }] }, context, [selection()])).toThrow();
  });
  it('detail validates both independent cursors, selection, binding and lack of authority', () => {
    const d = detail();
    d.tasks.page = { outcome: 'partial', revision: 100, scanned: 51, checkpoint: 0, next_cursor: token(1) };
    d.artifacts.page = { outcome: 'partial', revision: 100, scanned: 51, checkpoint: 0, next_cursor: token(2) };
    expect(parseMetadataDetail(d, context, selection(), { task_cursor: null, artifact_cursor: null }).tasks.page.next_cursor).toBe(token(1));
    expect(() => parseMetadataDetail(d, context, selection(), { task_cursor: token(1), artifact_cursor: null })).toThrow();
    expect(() => parseMetadataDetail({ ...d, cancellation_authority: true }, context, selection(), { task_cursor: null, artifact_cursor: null })).toThrow();
    expect(() => parseMetadataDetail({ ...d, selection: selection(2) }, context, selection(), { task_cursor: null, artifact_cursor: null })).toThrow();
    d.tasks.rows[0].binding = { task_id: 'wrong', generation: 1, lease_id: null, owner: null };
    expect(() => parseMetadataDetail(d, context, selection(), { task_cursor: null, artifact_cursor: null })).toThrow();
  });
});

describe('bounded reducer', () => {
  it.each([131, 1000])('advances %i rows one fixed page at a time with at most 200 retained', total => {
    let state: MetadataStreamState = initialMetadataStream(stream);
    let observations: MetadataObservation[] = [];
    let calls = 0;
    for (let offset = 0; offset < total; offset += 50) {
      const rows = Array.from({ length: Math.min(50, total - offset) }, (_, i) => summary(offset + i + 1));
      const raw = list(rows);
      raw.page = offset + rows.length < total
        ? { outcome: 'partial', revision: 10000, scanned: 51, checkpoint: 0, next_cursor: token(offset + 1) }
        : { outcome: 'complete', revision: 10000, scanned: rows.length, checkpoint: 10000, next_cursor: null };
      const parsed = parseMetadataList(raw, context, stream, state.traversal);
      const previous = state;
      state = advanceMetadataStream(state, parsed);
      observations = mergeMetadataRows(observations, parsed.rows).observations;
      expect(observations.length).toBeLessThanOrEqual(200);
      if (state.state === 'partial') { expect(state.checkpoint).toBe(0); expect(state.traversal.after_revision).toBe(previous.traversal.after_revision); }
      calls++;
    }
    expect(calls).toBe(Math.ceil(total / 50));
    expect(state.checkpoint).toBe(10000);
    expect(state.traversal).toEqual({ mode: 'changes', after_revision: 10000, cursor: null });
  });
  it('partial changes keep original after_revision, expired/unavailable keep checkpoint', () => {
    const old: MetadataStreamState = { ...initialMetadataStream(stream), state: 'complete', checkpoint: 7, traversal: { mode: 'changes', after_revision: 7, cursor: null } };
    const response = { ...list([]), mode: 'changes', page: { outcome: 'partial', revision: 13, scanned: 51, checkpoint: 7, next_cursor: token() } };
    const partial = advanceMetadataStream(old, parseMetadataList(response, context, stream, old.traversal));
    expect(partial.traversal.after_revision).toBe(7);
    expect(new URL(metadataListPath(context, stream, partial.traversal), 'http://local').searchParams.get('after_revision')).toBe('7');
    for (const outcome of ['unavailable', 'cursor_expired']) {
      const raw = { ...response, page: { outcome, revision: 0, scanned: 0, checkpoint: 7, next_cursor: null } };
      const next = advanceMetadataStream(partial, parseMetadataList(raw, context, stream, partial.traversal));
      expect(next.checkpoint).toBe(7); expect(next.traversal).toEqual(partial.traversal);
    }
  });
  it('same job IDs in different sources retain independent exact identity', () => {
    const one = summary(), other = { ...summary(), selection: { ...selection(), job_ref: { job_id: 'job_1', state_id: 'other-store' } } };
    let rows = mergeMetadataRows([], [one, other]).observations;
    expect(rows).toHaveLength(2);
    rows = mergeMetadataRows(rows, [{ selection: selection(), deleted: true, revision: 2 }]).observations;
    expect(rows).toHaveLength(1); expect(metadataSelectionKey(rows[0].row.selection)).toBe(metadataSelectionKey(other.selection));
    expect(mergeMetadataRows(rows, []).observations).toEqual(rows);
  });
  it('lower revision cannot overwrite an observed lifecycle', () => {
    const fresh = { ...summary(), revision: 10, lifecycle: 'done' };
    expect(mergeMetadataRows([{ row: fresh, freshness: 'observed' }], [summary()]).observations[0].row.lifecycle).toBe('done');
  });
  it('34-source bound and six sibling streams plus primary active lanes are explicit and repo excludes siblings', () => {
    const v = view();
    v.sources.push({ source: 'cli', state_id: 'primary', cross_project: false, available: true });
    v.sources.push(...Array.from({ length: 32 }, (_, i) => ({ source: 'cli', state_id: `sibling-${i}`, cross_project: true, available: true } satisfies MetadataView['sources'][number])));
    const parsed = parseMetadataView(v, context);
    expect(metadataStreams(parsed, 'all')).toHaveLength(206);
    expect(metadataStreams(parsed, 'repo')).toHaveLength(14);
    expect(() => parseMetadataView({ ...v, sources: [...v.sources, v.sources[0]] }, context)).toThrow();
  });
});

it('sibling status departures do not erase an equally recent successor status', () => {
  const current = { ...summary(), revision: 10, lifecycle: 'running' };
  const removed = { selection: selection(), revision: 10, deleted: true } satisfies MetadataRemoval;
  const result = mergeMetadataRows([{ row: current, freshness: 'observed' }], [removed], { status: 'started' });
  expect(result.observations).toHaveLength(1);
  expect(result.observations[0].row.lifecycle).toBe('running');
});
it('bounded removal revision guards reject older resurrection without keeping foreign fields', () => {
  const current = { ...summary(), revision: 10, lifecycle: 'running' };
  const removed = { selection: selection(), revision: 11, deleted: true } satisfies MetadataRemoval;
  const first = mergeMetadataRows([{ row: current, freshness: 'observed' }], [removed]);
  const replay = mergeMetadataRows(first.observations, [current], { removals: first.removals });
  expect(replay.observations).toEqual([]); expect(Object.keys(replay.removals[0]).sort()).toEqual(['deleted', 'revision', 'selection']);
});

it('parses real PM1.24 backend pages, signed cursor, details, pins and exact scope removal', () => {
  const c = { ...validateMetadataTarget({ session_id: backend.context.session_id, repo: backend.context.repo, scope: 'session' }), view_generation: backend.context.view_generation };
  const s = { store: { source: 'harness', state_id: backend.source.state_id }, status: null } satisfies Parameters<typeof initialMetadataStream>[0];
  const first = parseMetadataList(backend.first, c, s, initial);
  expect(first.rows).toHaveLength(50); expect(first.page.outcome).toBe('partial');
  const next = advanceMetadataStream(initialMetadataStream(s), first);
  const second = parseMetadataList(backend.second, c, s, next.traversal);
  expect(second.rows).toHaveLength(1); expect(second.page.outcome).toBe('complete');
  const selected = { ...backend.selection, source: 'harness' } satisfies Parameters<typeof parseMetadataDetail>[2];
  expect(parseMetadataDetail(backend.detail, c, selected, { task_cursor: null, artifact_cursor: null }).tasks.rows).toHaveLength(1);
  expect(parseMetadataPins(backend.pins, c, [selected]).results[0].result.kind).toBe('present');
  const changed = parseMetadataList(backend.changes, c, s, { mode: 'changes', cursor: null, after_revision: second.page.checkpoint });
  expect(changed.rows).toHaveLength(1); expect(changed.rows[0].deleted).toBe(true);
  expect(JSON.stringify(changed)).not.toContain('foreign-private');
});
it('status departure keeps a stale historical observation until an actual history deletion', () => {
  const current = { ...summary(), revision: 10, lifecycle: 'running' };
  const removed = { selection: selection(), revision: 11, deleted: true } satisfies MetadataRemoval;
  const activeRemoval = mergeMetadataRows([{ row: current, freshness: 'observed' }], [removed], { status: 'running' });
  expect(activeRemoval.observations).toEqual([{ row: current, freshness: 'stale' }]);
  const replay = mergeMetadataRows(activeRemoval.observations, [current], { removals: activeRemoval.removals });
  expect(replay.observations[0].freshness).toBe('stale');
  expect(mergeMetadataRows(replay.observations, [removed]).observations).toEqual([]);
});
