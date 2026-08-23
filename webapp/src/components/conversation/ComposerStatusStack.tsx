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
    return <Loader2 className="size-3 animate-spin text-muted-foreground/80" aria-hidden />;
  }
  if (row.state === "failed") {
    return <XCircle className="size-3 text-rose-500/85" aria-hidden />;
  }
  return <CheckCircle2 className="size-3 text-emerald-500/85" aria-hidden />;
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
      className="mx-2 mb-1 overflow-hidden rounded-lg border border-edge/70 bg-panel2/70"
      data-slot="composer-status-stack"
    >
      <div className="divide-y divide-edge/50">
        {grouped.map((group) => (
          <div key={group.kind} className="px-2 py-1">
            <div className="mb-0.5 flex items-center justify-between">
              <span className="text-[9px] font-semibold uppercase tracking-[0.16em] text-muted-foreground/65">
                {groupLabel(group.kind)}
              </span>
              <span className="text-[9px] font-mono text-muted-foreground/55">
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
                    className="flex w-full items-center gap-1.5 rounded-md border border-edge/50 bg-panel/60 px-1.5 py-1 text-left text-[11px] leading-4 text-txt/85 transition hover:border-edge2 hover:bg-panel2/70 focus-visible:border-accent/60 focus-visible:outline-none"
                  >
                    <span className="flex size-4 shrink-0 items-center justify-center rounded-full border border-edge/50 bg-panel text-[7px] font-semibold tracking-[0.12em] text-muted-foreground/70">
                      {rowKindLabel(row.kind)}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{row.label}</span>
                    <span className="flex shrink-0 items-center gap-1 text-[9px] uppercase tracking-[0.12em] text-muted-foreground/65">
                      {statusIcon(row)}
                      <span>{rowActionLabel(row)}</span>
                    </span>
                    <ChevronRight className="size-3 shrink-0 text-muted-foreground/45" aria-hidden />
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
