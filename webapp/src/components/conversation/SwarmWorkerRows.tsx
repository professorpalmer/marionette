import { formatTokenCount } from "../../lib/ledgerDisplay";
import { useOpenSwarmJob } from "../../lib/useOpenSwarmJob";
import { useSwarmWorkers } from "../../lib/swarmWorkers";

function statusTone(status: string): string {
  const s = status.toLowerCase();
  if (s === "running" || s === "in_progress" || s === "claimed") return "text-accent";
  if (s === "failed" || s === "error" || s === "cancelled") return "text-risk/85";
  if (s === "complete" || s === "completed" || s === "done") return "text-good/85";
  return "text-faint";
}

/**
 * One row per worker of a swarm: role, model, state, tokens, and the last
 * thing it did. A row opens the Swarm Tracker on that job.
 */
export function SwarmWorkerRows({ swarmKey, running }: { swarmKey: string; running: boolean }) {
  const workers = useSwarmWorkers(swarmKey, running);
  const openSwarmJob = useOpenSwarmJob();
  if (!workers || workers.rows.length === 0) return null;
  return (
    <ul className="ml-1 mt-0.5 mb-1 pl-3 border-l border-edge/50 space-y-px" data-testid="swarm-worker-rows">
      {workers.rows.map((row) => (
        <li key={row.taskId}>
          <button
            type="button"
            data-testid="swarm-worker-row"
            title="Open in Jobs"
            onClick={(e) => {
              e.stopPropagation();
              openSwarmJob(workers.jobId);
            }}
            className="w-full min-w-0 flex items-baseline gap-2 py-0.5 text-left text-ui-11 font-sans bg-transparent border-0 p-0 cursor-pointer group"
          >
            <span className="shrink-0 text-txt/85 group-hover:text-txt">{row.role}</span>
            {row.model ? <span className="shrink-0 text-faint">{row.model}</span> : null}
            <span className={`shrink-0 ${statusTone(row.status)}`}>{row.status}</span>
            {row.tokens !== null ? (
              <span className="shrink-0 text-faint tabular-nums">{formatTokenCount(row.tokens)} tok</span>
            ) : null}
            {row.lastAction ? (
              <span className="truncate min-w-0 text-faint/80 group-hover:text-muted">{row.lastAction}</span>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}
