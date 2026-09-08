import type { MetadataDetail } from '../lib/jobMetadata';
import { failedOutcomeStatuses } from './MetadataOutcomeChrome';

export default function MetadataWorkerProgress({ tasks }: { tasks: MetadataDetail['tasks'] }) {
  if (tasks.rows.length === 0) return null;
  const completed = tasks.rows.filter(task => ['complete', 'completed', 'done'].includes(task.status ?? '')).length;
  const failed = tasks.rows.filter(task => failedOutcomeStatuses.has(task.status ?? '')).length;
  const cancelled = tasks.rows.filter(task => task.status === 'cancelled').length;
  const partial = tasks.rows.filter(task => task.status === 'partial').length;
  // These are PM task records; PM stalled is recoverable terminal, unlike native stalled.
  const recoverable = tasks.rows.filter(task => task.status === 'stalled').length;
  const finished = completed + failed + cancelled + partial + recoverable;
  return <div aria-label="Workers" className="text-xs text-muted">
    <p>Workers ({tasks.rows.length})</p>
    <p>{finished}/{tasks.rows.length} observed workers finished · {completed} completed · {failed} failed{cancelled ? ` · ${cancelled} cancelled` : ''}{partial ? ` · ${partial} partial` : ''}{recoverable ? ` · ${recoverable} recoverable` : ''}</p>
    {tasks.page.outcome !== 'complete' && <p>Counts cover only the shown task records; task coverage is incomplete.</p>}
  </div>;
}
