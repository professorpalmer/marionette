import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ChevronDown, ChevronRight, CheckCircle2, Loader2, X, XCircle } from "lucide-react";
import { api, type Job } from "../../lib/api";
import { openAgentCommand, openAgentSwarmJob } from "../../lib/agentLinks";
import {
  dismissAgentCommandSession,
  getAgentCommandIndexVersion,
  listAgentCommandSessions,
  subscribeAgentCommandIndex,
} from "../../lib/agentCommandIndex";
import { buildComposerStatusStackRows, type ComposerStatusStackRow } from "./composerStatusStackData";
import { COMPOSER_FAMILY_HAIRLINE, COMPOSER_FAMILY_LABEL, COMPOSER_FAMILY_SECTION } from "./composerFamily";

const ROW_FOCUS =
  "focus-visible:border-accent/60 focus-visible:outline-none";
const ICON_FOCUS =
  "focus:outline-none focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-1 focus-visible:outline-accent";

function statusIcon(row: ComposerStatusStackRow) {
  if (row.state === "running") {
    return <Loader2 size={11} className="animate-spin text-faint" aria-hidden />;
  }
  if (row.state === "failed") {
    return <XCircle size={11} className="text-risk" aria-hidden />;
  }
  return <CheckCircle2 size={11} className="text-good" aria-hidden />;
}

function groupLabel(kind: ComposerStatusStackRow["kind"]): string {
  return kind === "swarm" ? "Puppetmaster" : "Terminal";
}

function stopAllLabel(kind: ComposerStatusStackRow["kind"]): string {
  return kind === "swarm" ? "Stop all Puppetmaster jobs" : "Stop all commands";
}

function stopRowLabel(kind: ComposerStatusStackRow["kind"]): string {
  return kind === "swarm" ? "Stop job" : "Stop command";
}

function StatusStackGroup({
  kind,
  rows,
  cancelling,
  onCancel,
}: {
  kind: ComposerStatusStackRow["kind"];
  rows: ComposerStatusStackRow[];
  cancelling: ReadonlySet<string>;
  onCancel: (ids: string[]) => void;
}) {
  const runningIds = rows.filter((row) => row.state === "running").map((row) => row.id);
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? runningIds.length > 0;

  return (
    <div>
      <div className="flex items-center">
        <button
          type="button"
          aria-label={groupLabel(kind)}
          aria-expanded={open}
          onClick={() => setUserOpen(!open)}
          className={`flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1 text-left text-[10.5px] leading-4 text-txt hover:bg-panel/35 ${ICON_FOCUS}`}
        >
          {open
            ? <ChevronDown size={11} className="shrink-0 text-faint" aria-hidden />
            : <ChevronRight size={11} className="shrink-0 text-faint" aria-hidden />}
          <span className={`${COMPOSER_FAMILY_LABEL} font-medium`}>{groupLabel(kind)}</span>
          <span className="ml-auto font-mono text-[10.5px] text-muted tabular-nums">
            {rows.length}
          </span>
        </button>
        {runningIds.length > 0 && (
          runningIds.every((id) => cancelling.has(id)) ? (
            <span className="shrink-0 px-1.5 text-[9px] italic text-risk/70">cancelling...</span>
          ) : (
            <button
              type="button"
              title={stopAllLabel(kind)}
              aria-label={stopAllLabel(kind)}
              onClick={() => onCancel(runningIds)}
              className={`mr-1.5 shrink-0 text-faint/50 hover:text-risk ${ICON_FOCUS}`}
            >
              <X size={11} />
            </button>
          )
        )}
      </div>
      {open && (
        <div className="space-y-0.5 px-2 pb-1.5">
          {rows.map((row) => {
            const stopping = cancelling.has(row.id);
            const openLabel = row.kind === "swarm" ? "Open swarm" : "Open terminal";
            const onOpen = () => {
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
              <div key={`${row.kind}:${row.id}`} className="flex items-center gap-0.5">
                <button
                  type="button"
                  onClick={onOpen}
                  aria-label={`${openLabel}: ${row.label}`}
                  className={`flex min-w-0 flex-1 items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-[10.5px] leading-4 text-txt transition-colors hover:bg-panel/25 ${ROW_FOCUS}`}
                >
                  {statusIcon(row)}
                  <span className="min-w-0 flex-1 truncate">{row.label}</span>
                  <ChevronRight size={11} className="shrink-0 text-faint" aria-hidden />
                </button>
                {row.state === "running" && (
                  stopping ? (
                    <span className="shrink-0 px-1 text-[9px] italic text-risk/70">cancelling...</span>
                  ) : (
                    <button
                      type="button"
                      title={stopRowLabel(row.kind)}
                      aria-label={stopRowLabel(row.kind)}
                      onClick={() => onCancel([row.id])}
                      className={`shrink-0 text-faint/50 hover:text-risk ${ICON_FOCUS}`}
                    >
                      <X size={11} />
                    </button>
                  )
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function ComposerStatusStack({
  swarmJobs,
  sessionId = "",
}: {
  swarmJobs: readonly Job[];
  sessionId?: string;
}) {
  const [nowTick, setNowTick] = useState(() => Date.now());
  const [cancelling, setCancelling] = useState<Set<string>>(() => new Set());
  const commandIndexVersion = useSyncExternalStore(
    subscribeAgentCommandIndex,
    getAgentCommandIndexVersion,
    getAgentCommandIndexVersion,
  );
  const commandSessions = useMemo(
    () => listAgentCommandSessions(sessionId),
    [commandIndexVersion, sessionId],
  );
  const rows = useMemo(
    () => buildComposerStatusStackRows({
      swarmJobs,
      commandSessions,
      nowMs: nowTick,
      sessionId,
    }),
    [commandSessions, nowTick, sessionId, swarmJobs],
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

  const onCancel = (ids: string[]) => {
    const unique = [...new Set(ids.filter(Boolean))];
    if (!unique.length) return;
    setCancelling((prev) => {
      const next = new Set(prev);
      for (const id of unique) next.add(id);
      return next;
    });
    for (const id of unique) dismissAgentCommandSession(id);
    void Promise.allSettled(unique.map((id) => api.swarmCancel(id))).then((results) => {
      setCancelling((prev) => {
        const next = new Set(prev);
        results.forEach((result, i) => {
          const id = unique[i];
          if (result.status === "rejected") {
            next.delete(id);
            return;
          }
          if (result.value.ok === false) next.delete(id);
        });
        return next;
      });
    });
  };

  if (grouped.length === 0) return null;

  return (
    <div
      className={COMPOSER_FAMILY_SECTION}
      data-slot="composer-status-stack"
    >
      <div className={`divide-y ${COMPOSER_FAMILY_HAIRLINE}`}>
        {grouped.map((group) => (
          <StatusStackGroup
            key={group.kind}
            kind={group.kind}
            rows={group.rows}
            cancelling={cancelling}
            onCancel={onCancel}
          />
        ))}
      </div>
    </div>
  );
}
