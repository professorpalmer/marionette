import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ChevronRight, CheckCircle2, Loader2, XCircle } from "lucide-react";
import { openAgentCommand, openAgentSwarmJob } from "../../lib/agentLinks";
import {
  getAgentCommandIndexVersion,
  listAgentCommandSessions,
  subscribeAgentCommandIndex,
} from "../../lib/agentCommandIndex";
import { buildComposerStatusStackRows, type ComposerStatusStackRow } from "./composerStatusStackData";
import { COMPOSER_FAMILY_LABEL, COMPOSER_FAMILY_SURFACE } from "./composerFamily";
import type { Job } from "../../lib/api";

function statusIcon(row: ComposerStatusStackRow) {
  if (row.state === "running") {
    return <Loader2 size={11} className="animate-spin text-faint" aria-hidden />;
  }
  if (row.state === "failed") {
    return <XCircle size={11} className="text-risk" aria-hidden />;
  }
  return <CheckCircle2 size={11} className="text-good" aria-hidden />;
}

function rowKindLabel(kind: ComposerStatusStackRow["kind"]): string {
  return kind === "swarm" ? "PM" : "Term";
}

function rowActionLabel(row: ComposerStatusStackRow): string {
  if (row.kind === "swarm") return "Open swarm";
  return "Open terminal";
}

function groupLabel(kind: ComposerStatusStackRow["kind"]): string {
  return kind === "swarm" ? "Puppetmaster" : "Terminal";
}

export default function ComposerStatusStack({ swarmJobs }: { swarmJobs: readonly Job[] }) {
  const [nowTick, setNowTick] = useState(() => Date.now());
  const commandIndexVersion = useSyncExternalStore(
    subscribeAgentCommandIndex,
    getAgentCommandIndexVersion,
    getAgentCommandIndexVersion,
  );
  const commandSessions = useMemo(
    () => listAgentCommandSessions(),
    [commandIndexVersion],
  );
  const rows = useMemo(
    () => buildComposerStatusStackRows({ swarmJobs, commandSessions, nowMs: nowTick }),
    [commandSessions, nowTick, swarmJobs],
  );
  const hasTerminalRows = rows.some((row) => row.state !== "running");

  useEffect(() => {
    if (!rows.length || !hasTerminalRows) return;
    const timer = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasTerminalRows, rows.length]);

  const grouped = useMemo(() => {
    const out: Array<{ kind: ComposerStatusStackRow["kind"]; rows: ComposerStatusStackRow[] }> = [];
    for (const row of rows) {
      const bucket = out[out.length - 1];
      if (bucket && bucket.kind === row.kind) {
        bucket.rows.push(row);
      } else {
        out.push({ kind: row.kind, rows: [row] });
      }
    }
    return out;
  }, [rows]);

  if (grouped.length === 0) return null;

  return (
    <div
      className={`mx-2 mb-1 overflow-hidden ${COMPOSER_FAMILY_SURFACE}`}
      data-slot="composer-status-stack"
    >
      <div className="divide-y divide-edge/50">
        {grouped.map((group) => (
          <div key={group.kind} className="px-2 py-1">
            <div className="mb-0.5 flex items-center justify-between">
              <span className={`${COMPOSER_FAMILY_LABEL} font-medium`}>
                {groupLabel(group.kind)}
              </span>
              <span className="text-[10.5px] font-mono text-muted">
                {group.rows.length}
              </span>
            </div>
            <div className="space-y-0.5">
              {group.rows.map((row) => {
                const onClick = () => {
                  if (row.kind === "swarm") {
                    openAgentSwarmJob(row.id);
                    return;
                  }
                  openAgentCommand(row.command || row.label, {
                    id: row.id,
                    output: row.output || "",
                  });
                };

                return (
                  <button
                    key={`${row.kind}:${row.id}`}
                    type="button"
                    onClick={onClick}
                    title={row.title}
                    className="flex w-full items-center gap-1.5 rounded-md border border-edge/50 bg-panel/60 px-1.5 py-1 text-left text-[10.5px] leading-4 text-txt transition hover:border-edge2 hover:bg-panel2/70 focus-visible:border-accent/60 focus-visible:outline-none"
                  >
                    <span className="flex h-[20px] shrink-0 items-center rounded-md border border-edge/30 bg-panel2/40 px-1 text-[10.5px] font-medium text-faint">
                      {rowKindLabel(row.kind)}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{row.label}</span>
                    <span className="flex shrink-0 items-center gap-1 text-[10.5px] text-muted">
                      {statusIcon(row)}
                      <span>{rowActionLabel(row)}</span>
                    </span>
                    <ChevronRight size={11} className="shrink-0 text-faint" aria-hidden />
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
