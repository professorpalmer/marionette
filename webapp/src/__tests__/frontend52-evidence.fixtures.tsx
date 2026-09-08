import { fireEvent, waitFor } from '@testing-library/react';
import { expect } from 'vitest';
import { identityFixture } from './frontend52-identity.fixtures';
import { parseMetadataDetail } from '../lib/jobMetadata';
import type { MetadataDetail } from '../lib/jobMetadata';
import type { MetadataDisplay } from '../lib/selectedMetadataEvidence';
import { expertDetail, expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import type { MetadataSelection } from '../lib/jobMetadata';

export const evidenceSelection: MetadataSelection = {
  job_ref: { job_id: 'job_frontend52-evidence', state_id: 'state-evidence' },
  source: 'harness', session_id: 'sess-test', repo: '/workspace',
};
export const evidenceHash = 'e'.repeat(64);

export async function evidenceFixture(goal: string, selections = [evidenceSelection]) {
  const fixture = await expertMetadataFixture(selections.map(selected => ({
    ...expertSummary(selected, selected === selections[0] ? goal : 'CLI evidence job'), lifecycle: 'complete',
  })));
  fixture.selected.mockImplementation(async selected => {
    const detail = expertDetail(selected, fixture.context());
    return { ...detail, lifecycle: 'complete', artifact_count: 1,
      artifacts: { ...detail.artifacts, rows: [{ ...detail.artifacts.rows[0],
        id: selected.source === 'cli' ? 'cli-finding' : 'harness-finding',
        sha256: selected.source === 'cli' ? 'c'.repeat(64) : evidenceHash,
      }] } };
  });
  return fixture;
}

export async function qualityFixture(options: Parameters<typeof identityFixture>[0], taskId: string | null) {
  const fixture = await identityFixture(options);
  const selected = fixture.store.getSnapshot().detail;
  if (selected.kind !== 'selected' || !selected.observation) throw Error('Missing selected evidence');
  const detail = { ...selected.observation,
    display: { kind: 'available', goal_preview: 'Identity evidence', goal_preview_truncated: false,
      delivery: 'unverified', quality: 'unverified' } satisfies MetadataDisplay,
    artifact_count: 1,
    artifacts: { ...selected.observation.artifacts, rows: [{ ...selected.observation.artifacts.rows[0],
      id: 'verification-record', type: 'verification', task_id: taskId, status: 'complete', sha256: evidenceHash,
    }] },
  };
  fixture.selected.mockResolvedValue(detail);
  fireEvent.click(fixture.inspect);
  await waitFor(() => {
    const current = fixture.store.getSnapshot().detail;
    if (current.kind !== 'selected' || current.observation?.artifacts.rows[0]?.id !== 'verification-record') throw Error('Evidence not refreshed');
  });
  return { ...fixture, detail };
}

export function rejectQualityVerdicts(detail: MetadataDetail) {
  const cursors = { task_cursor: null, artifact_cursor: null };
  expect(parseMetadataDetail(detail, detail.context, detail.selection, cursors).artifacts.rows[0].id).toBe('verification-record');
  for (const verdict of ['failed', 'degraded', 'passed']) {
    expect(() => parseMetadataDetail({ ...detail, artifacts: { ...detail.artifacts,
      rows: [{ ...detail.artifacts.rows[0], check_result: verdict }] } }, detail.context, detail.selection, cursors)).toThrow('invalid_metadata');
    expect(() => parseMetadataDetail({ ...detail, artifacts: { ...detail.artifacts,
      rows: [{ ...detail.artifacts.rows[0], result: verdict }] } }, detail.context, detail.selection, cursors)).toThrow('invalid_metadata');
  }
  for (const quality of ['degraded', 'ok']) expect(() => parseMetadataDetail({ ...detail,
    display: { kind: 'available', goal_preview: 'Unsupported certification', goal_preview_truncated: false,
      delivery: 'unverified', quality },
  }, detail.context, detail.selection, cursors)).toThrow('invalid_metadata');
  expect(parseMetadataDetail({ ...detail, display: { ...detail.display, trustworthy: true } },
    detail.context, detail.selection, cursors).display).toEqual(detail.display);
}
