import type { MetadataDetail } from '../lib/jobMetadata';

export default function MetadataFindingCount({ detail }: { detail: MetadataDetail }) {
  const findings = detail.artifacts.rows.filter(row => row.type?.toLowerCase() === 'finding');
  const count = new Set(findings.map(row => row.sha256 ? `sha256:${row.sha256}` : `id:${row.id}`)).size;
  if (!count) return null;
  const complete = detail.artifacts.page.outcome === 'complete'
    && detail.artifact_count === detail.artifacts.rows.length;
  return <p>Findings ({count}{complete ? '' : ' on this page'})</p>;
}
