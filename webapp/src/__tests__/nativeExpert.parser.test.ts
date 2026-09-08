import { expect, it } from 'vitest';
import wire from './nativeExpert.backend.json';
import { parseLocalDetail } from '../lib/localJobMetadata';
import type { LocalDetail } from '../lib/localJobMetadata';
import type { MetadataContext } from '../lib/jobMetadata';
const context: MetadataContext = { ...wire.tasks.context, scope: 'session' };
const selected = wire.tasks.local_ref;
const parse = (value: unknown, lane: LocalDetail['lane'] = 'tasks') => parseLocalDetail(value, context, selected, lane);
it('parses the actual producer task and forecast without promoting model authority', () => {
  const tasks = parse(wire.tasks), routing = parse(wire.routing, 'routing');
  expect(tasks.lane).toBe('tasks');
  if (tasks.lane !== 'tasks' || routing.lane !== 'routing') throw Error('Wrong lane');
  expect(tasks.rows[0]).toMatchObject({ task_id: `${selected.job_id}-w0`, instruction: 'Keyboard disclosure', model: '', model_kind: 'unavailable' });
  expect(routing.rows[0]).toMatchObject({ model: 'cheap-model', model_kind: 'forecast', policy: 'balanced', created_by: 'router', association: 'explicit' });
  expect(tasks.summary).not.toHaveProperty('goal_preview');
  expect(tasks.summary?.display?.label).toBe('Provider worker');
  expect(tasks.summary?.display?.model).toBe('');
});
it.each([
  { role: 'x'.repeat(161) }, { instruction: 'x'.repeat(1025) }, { model: 'x'.repeat(161) },
  { adapter: 'x'.repeat(41) }, { status: 'x'.repeat(41) }, { task_id: 'x'.repeat(257) },
  { task_id: 'bad/id' }, { task_id: {} }, { model_kind: 'forecast' }, { model: 'invented' },
  { model_kind: 'assigned', model: '' }, { model_kind: 'realized', model: 'configured' }, { truncated: 'false' },
])('rejects invalid task scalars %#', change => {
  expect(() => parse({ ...wire.tasks, rows: [{ ...wire.tasks.rows[0], ...change }] })).toThrow();
});
it.each([
  { policy: 'x'.repeat(41) }, { detail: 'x'.repeat(513) }, { ordinal: -1 }, { ordinal: 0.5 },
  { task_id: null }, { association: 'guessed' }, { association: 'unavailable' },
  { model_kind: 'current' }, { est_cost_usd: -1 }, { created_by: {} },
])('rejects malformed routing facts %#', change => {
  expect(() => parse({ ...wire.routing, rows: [{ ...wire.routing.rows[0], ...change }] }, 'routing')).toThrow();
});
it('retains unavailable task identities without inventing a shortened binding', () => {
  const value = parse({ ...wire.tasks, rows: [{ ...wire.tasks.rows[0], task_id: null }] });
  expect(value.rows[0]).toHaveProperty('task_id', null);
});
it('rejects unordered routing and mismatched revision, owner, lane and context', () => {
  expect(() => parse({ ...wire.routing, rows: [wire.routing.rows[0], wire.routing.rows[0]], page: { ...wire.routing.page, scanned: 2 } }, 'routing')).toThrow();
  expect(() => parse({ ...wire.tasks, summary: { ...wire.tasks.summary, revision: 3 } })).toThrow();
  expect(() => parse({ ...wire.tasks, local_ref: { ...selected, incarnation: 'other' } })).toThrow();
  expect(() => parse({ ...wire.tasks, context: { ...context, view_generation: 'other' } })).toThrow();
  expect(() => parse(wire.tasks, 'routing')).toThrow();
});

it.each([
  ['tasks', wire.tasks, 'tasks'],
  ['configured task', wire.configured_tasks, 'tasks'],
  ['forecast', wire.routing, 'routing'],
  ['reconciled route', wire.reconciled_routing, 'routing'],
] as const)('preserves every emitted %s row exactly through the parser', (_name, response, lane) => {
  const parsed = parseLocalDetail(response, { ...response.context, scope: 'session' }, response.local_ref, lane);
  expect(parsed.rows).toEqual(response.rows);
});
it('keeps pre-execution task assignment distinct from realized routing', () => {
  const response = wire.configured_tasks;
  const parsed = parseLocalDetail(response, { ...response.context, scope: 'session' }, response.local_ref, 'tasks');
  expect(parsed.rows[0]).toMatchObject({ model: 'native/configured-model', model_kind: 'assigned' });
  expect(parse(wire.reconciled_routing, 'routing').rows[0]).toHaveProperty('model_kind', 'realized');
});

it('drops obsolete native instruction previews at the parser boundary', () => {
  const parsed = parse({ ...wire.tasks, summary: { ...wire.tasks.summary,
    goal_preview: { text: wire.tasks.rows[0].instruction, truncated: false } } });
  expect(parsed.summary).not.toHaveProperty('goal_preview');
  expect(parsed.summary?.display?.label).toBe('Provider worker');
});
