import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ChevronRight, CheckCircle2, Loader2, XCircle } from "lucide-react";
import { openAgentCommand, openAgentSwarmJob } from "../../lib/agentLinks";
import {
  getAgentCommandIndexVersion,
  listAgentCommandSessions,
  subscribeAgentCommandIndex,
} from "../../lib/agentCommandIndex";
import { buildComposerStatusStackRows, type ComposerStatusStackRow } from "./composerStatusStackData";
import type { Job } from "../../lib/api";

function statusIcon(row: ComposerStatusStackRow) {
  if (row.state === "running") {
    return <Loader2 className="size-3.5 animate-spin text-muted-foreground/80" aria-hidden />;
  }
  if (row.state === "failed") {
    return <XCircle className="size-3.5 text-rose-500/85" aria-hidden />;
  }
  return <CheckCircle2 className="size-3.5 text-emerald-500/85" aria-hidden />;
}

function rowKindLabel(kind: ComposerStatusStackRow["kind"]): string {
  return kind === "swarm" ? "PM" : "TERMINAL";
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
      className="mx-2 mb-2 overflow-hidden rounded-2xl border border-edge/80 bg-panel2/80 shadow-lg shadow-black/15"
      data-slot="composer-status-stack"
    >
      <div className="divide-y divide-edge/60">
        {grouped.map((group) => (
          <div key={group.kind} className="px-2.5 py-2">
            <div className="mb-1 flex items-center justify-between px-0.5">
              <span className="text-[9px] font-semibold uppercase tracking-[0.22em] text-muted-foreground/70">
                {groupLabel(group.kind)}
              </span>
              <span className="text-[9px] font-mono text-muted-foreground/60">
                {group.rows.length}
              </span>
            </div>
            <div className="space-y-1">
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
                    className="flex w-full items-center gap-2 rounded-xl border border-edge/60 bg-panel/70 px-2.5 py-1.5 text-left text-[11px] leading-4 text-txt/90 transition hover:border-edge2 hover:bg-panel2/80 focus-visible:border-accent/60 focus-visible:outline-none"
                  >
                    <span className="flex size-5 shrink-0 items-center justify-center rounded-full border border-edge/60 bg-panel text-[8px] font-semibold tracking-[0.18em] text-muted-foreground/70">
                      {rowKindLabel(row.kind)}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{row.label}</span>
                    <span className="flex shrink-0 items-center gap-1 text-[9px] uppercase tracking-[0.18em] text-muted-foreground/70">
                      {statusIcon(row)}
                      <span>{rowActionLabel(row)}</span>
                    </span>
                    <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/50" aria-hidden />
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
