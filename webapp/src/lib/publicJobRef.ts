/** A store incarnation is part of identity, including on acknowledgements. */
export type PublicJobRef = { job_id: string; state_id: string } & (
  | { version: 2; incarnation: string }
  | { version?: never; incarnation?: never }
);
export function jobRefKey(ref: PublicJobRef): string {
  return JSON.stringify([ref.job_id, ref.state_id, ref.version ?? null, ref.incarnation ?? null]);
}
export function jobRefQuery(ref: PublicJobRef): Record<string, string> {
  return { job_id: ref.job_id, state_id: ref.state_id,
    ...(ref.version === 2 ? { version: '2', incarnation: ref.incarnation } : {}) };
}
export function isPublicJobRef(value: unknown): value is PublicJobRef {
  if (!value || typeof value !== 'object' || !('job_id' in value) || !('state_id' in value)
    || typeof value.job_id !== 'string' || !/^[\x00-\x7f]{1,256}$/.test(value.job_id)
    || typeof value.state_id !== 'string' || !/^[\x00-\x7f]{1,256}$/.test(value.state_id)) return false;
  const keys = Object.keys(value);
  if (keys.some(key => !['job_id', 'state_id', 'version', 'incarnation'].includes(key))) return false;
  return 'version' in value || 'incarnation' in value
    ? 'version' in value && value.version === 2 && 'incarnation' in value
      && typeof value.incarnation === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value.incarnation)
    : true;
}
