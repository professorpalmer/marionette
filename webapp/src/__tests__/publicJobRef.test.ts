import { expect, it } from 'vitest';
import { isPublicJobRef, jobRefKey, jobRefQuery } from '../lib/publicJobRef';

const legacy = { job_id: 'job_a', state_id: 'state_a' };
const current = { ...legacy, version: 2 as const, incarnation: '11111111-1111-4111-8111-111111111111' };

it('preserves exported legacy and incarnation-bound identities', () => {
  expect(isPublicJobRef(legacy)).toBe(true);
  expect(isPublicJobRef(current)).toBe(true);
  expect(isPublicJobRef({ ...legacy, job_id: 'a'.repeat(256) })).toBe(true);
  expect(jobRefKey(current)).not.toEqual(jobRefKey(legacy));
  expect(jobRefKey(current)).not.toEqual(jobRefKey({ ...current, incarnation: '22222222-2222-4222-8222-222222222222' }));
  expect(jobRefQuery(current)).toEqual({ ...current, version: '2' });
});

it.each([
  { ...current, incarnation: 'not-a-uuid' },
  { ...current, incarnation: 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA' },
  { ...legacy, version: 2 },
  { ...legacy, incarnation: current.incarnation },
  { ...current, version: 1 },
  { ...current, extra: 'unexpected' },
  { ...legacy, job_id: '' },
  { ...legacy, job_id: 'a'.repeat(257) },
  { ...legacy, state_id: 'é' },
])('rejects malformed public identity %#', value => {
  expect(isPublicJobRef(value)).toBe(false);
});
