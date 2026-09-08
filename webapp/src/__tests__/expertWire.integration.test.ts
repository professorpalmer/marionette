import { describe, expect, it } from 'vitest';
import captures from './expertWire.backend.json';
import { parseMetadataDetail, parseMetadataList, parseMetadataPins, parseMetadataSelection } from '../lib/jobMetadata';
import type { MetadataContext } from '../lib/jobMetadata';

for (const capture of captures) {
  describe(`${capture.backend} kernel to inspector wire`, () => {
    const context = { ...capture.context, scope: 'session' } satisfies MetadataContext;
    const selected = () => parseMetadataSelection(capture.selection, context);
    it('retains the exact incarnation through list and pins', () => {
      const selection = selected();
      const stream = { store: { source: 'harness' as const, state_id: capture.stream.store.state_id }, status: null };
      const result = parseMetadataList(capture.listing, context, stream, { mode: 'snapshot', cursor: null, after_revision: 0 });
      expect(result.rows).toHaveLength(1);
      expect(result.rows[0].selection).toEqual(selection);
      const pins = parseMetadataPins(capture.pins, context, [selection]);
      expect(pins.results[0].result.kind).toBe('present');
      expect(pins.results[0].selection.job_ref).toEqual(capture.selection.job_ref);
    });
    it('accepts actual history and terminal economics without inventing checks', () => {
      const result = parseMetadataDetail(capture.detail, context, selected(), { task_cursor: null, artifact_cursor: null });
      expect(result.display).toEqual(capture.detail.display);
      expect(result.cost).toEqual(capture.detail.cost);
      expect(result.history.kind).toBe('available');
      if (result.history.kind !== 'available') throw new Error('Real captured history was discarded');
      expect(result.history.attempts.rows[0].facts.model).toBe('gpt-6-astra');
      expect(result.history.observations.rows[0].facts.tokens_out).toBe(0);
      expect(result.history.counts.complete_invocation_history).toBe(false);
      expect(result.artifacts.rows[0].check_result).toBe('unavailable');
      expect(result.cancellation_authority).toBe(false);
    });
    it('rejects evidence from a different store incarnation', () => {
      const altered = structuredClone(capture.detail);
      altered.history.attempts.rows[0].job_ref.incarnation = '00000000-0000-0000-0000-000000000000';
      expect(() => parseMetadataDetail(altered, context, selected(), { task_cursor: null, artifact_cursor: null })).toThrow();
    });
  });
}
