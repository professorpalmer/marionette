import { expect, it } from 'vitest';
import wire from './nativeSelectedContext.backend.json';
import { parseLocalDetail, parseLocalList } from '../lib/localJobMetadata';
import { context } from './jobMetadata.fixtures';

const selected = wire.detail.local_ref;
function parse(value: unknown) { return parseLocalDetail(value, context, selected, 'children', true); }
it('parses real owned request and newer cancelled summary with unavailable lane independently', () => {
  const initial = parse(wire.children);
  const cancelled = parse(wire.cancelled);
  expect(initial.selected_context?.request?.text).toBe('printf selected-context');
  expect(cancelled.summary?.lifecycle).toBe('cancelled');
  expect(cancelled.page.outcome).toBe('unavailable');
  expect(cancelled.summary?.revision).toBeGreaterThan(initial.summary?.revision ?? 0);
  expect(cancelled.summary?.display?.model).toBe('');
  expect(parseLocalList(wire.history, context, selected.incarnation, { mode: 'snapshot', after_revision: 0, cursor: null }).rows[0]).not.toHaveProperty('selected_context');
});
it('rejects context on non-explicit reads', () => {
  expect(() => parseLocalDetail(wire.children, context, selected, 'children')).toThrow();
});
it.each([
  { ...wire.children.selected_context, request: { text: 'é'.repeat(1025), truncated: true } },
  { ...wire.children.selected_context, cwd: { text: 'é'.repeat(257), truncated: true } },
  { ...wire.children.selected_context, request: { text: 'ok', truncated: 'false' } },
  { ...wire.children.selected_context, request: { text: { secret: 'nested' }, truncated: false } },
  { ...wire.children.selected_context, source: 'goal' },
  { ...wire.children.selected_context, omission: 'none' },
  { ...wire.children.selected_context, provider_payload: {} },
])('rejects malformed or oversized selected projection %#', selected_context => {
  expect(() => parse({ ...wire.children, selected_context })).toThrow();
});
it.each([
  { ...wire.children, context: { ...context, view_generation: 'other' } },
  { ...wire.children, local_ref: { ...selected, incarnation: 'other' } },
  { ...wire.children, summary: { ...wire.children.summary, local_ref: { ...selected, job_id: 'local-other' } } },
  { ...wire.children, summary: { ...wire.children.summary, session_id: 'other' } },
  { ...wire.children, page: { ...wire.children.page, outcome: 'expired' } },
])('rejects changed owner/context or expired selected context %#', value => {
  expect(() => parse(value)).toThrow();
});
