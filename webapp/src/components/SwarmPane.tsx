import { useEffect, useState } from "react";
import { Loader2, CheckCircle2, XCircle, Circle, ChevronDown, ChevronRight, Cpu, Activity, Network } from "lucide-react";
import { api, type SwarmLive, type Job, type Artifact, type Task } from "../lib/api";

type Status = "pending" | "in_progress" | "completed" | "cancelled";

// Turn the router's raw policy string ("policy=balanced: cheapest sufficient
// model whose capability_score (99) >= needed (50) (plan-billed...)") into one
// plain-language sentence. The raw string + a per-model rejection wall reads
// like an error dump; this reads like a decision.
function summarizeRouting(art: Artifact): string {
  const detail = typeof art.detail === "string" ? art.detail : "";
  const policy = (detail.match(/policy=(\w+)/) || [])[1] || "";
  const planBilled = /plan-billed|in-subscription/i.test(detail);
  const lead: Record<string, string> = {
    balanced: "Right-sized: cheapest model that clears the task's need",
    cheap: "Cheapest available model",
    quality: "Highest-capability model for the task",
    escalating: "Cheapest sufficient model, escalates if it stalls",
  };
  const base = lead[policy] || "Router pick";
  return planBilled ? `${base} \u00b7 plan-billed, no marginal cost` : base;
}

function jobStatus(j: Job): Status {
  const s = (j.status || "").toLowerCase();
  if (s.includes("complete") || s.includes("done")) return "completed";
  if (s.includes("fail") || s.includes("cancel") || s.includes("error")) return "cancelled";
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "in_progress";
  return "pending";
}

function taskState(t: Task): "running" | "done" | "fail" | "idle" {
  const s = (t.status || "").toLowerCase();
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "running";
  if (s.includes("complete") || s.includes("done")) return "done";
  if (s.includes("fail") || s.includes("cancel") || s.includes("error")) return "fail";
  return "idle";
}

// The four visible phases of a swarm's life. A job advances left-to-right; the
// strip fills behind the active phase so a running swarm reads as *moving*
// instead of a static spinner. "failed" paints the reached phase red.
const PHASES = ["dispatched", "routing", "workers", "done"] as const;

function jobPhase(j: Job): { key: string; label: string; index: number; failed: boolean } {
  const st = jobStatus(j);
  const tasks = j.tasks || [];
  const total = tasks.length;
  const running = tasks.filter((t) => taskState(t) === "running").length;
  const doneCount = tasks.filter((t) => taskState(t) === "done").length;
  const hasRouting = (j.artifacts || []).some((a) => (a.type || "").toUpperCase() === "ROUTING");

  if (st === "cancelled") {
    const reached = total > 0 ? 2 : hasRouting ? 1 : 0;
    return { key: "failed", label: "failed", index: reached, failed: true };
  }
  if (st === "completed") return { key: "done", label: "done", index: 3, failed: false };
  if (total > 0 && running > 0) return { key: "workers", label: `running ${doneCount}/${total}`, index: 2, failed: false };
  if (total > 0) return { key: "workers", label: `${total} worker${total > 1 ? "s" : ""}`, index: 2, failed: false };
  if (hasRouting) return { key: "routing", label: "routing", index: 1, failed: false };
  return { key: "dispatched", label: "dispatched", index: 0, failed: false };
}

function PhaseStrip({ job }: { job: Job }) {
  const { index, failed, key } = jobPhase(job);
  const active = key !== "done" && !failed;
  return (
    <div className="flex items-center gap-1 mt-1.5" title={PHASES.join(" -> ")}>
      {PHASES.map((_, i) => {
        const reached = i <= index;
        const isActiveSeg = i === index && active;
        const color = failed && i === index
          ? "bg-risk"
          : reached
          ? (key === "done" ? "bg-good" : "bg-accent")
          : "bg-edge/60";
        return (
          <div
            key={i}
            className={`h-1 flex-1 rounded-full transition-all ${color} ${isActiveSeg ? "animate-pulse" : ""}`}
          />
        );
      })}
    </div>
  );
}

function WorkerProgress({ tasks }: { tasks: Task[] }) {
  const total = tasks.length;
  if (total === 0) return null;
  const done = tasks.filter((t) => taskState(t) === "done").length;
  const failed = tasks.filter((t) => taskState(t) === "fail").length;
  const pct = Math.round(((done + failed) / total) * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-panel2 border border-edge/50 rounded-full overflow-hidden">
        <div className="h-full bg-good transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[9px] text-faint tabular-nums shrink-0">{done + failed}/{total}</span>
    </div>
  );
}

export default function SwarmPane() {
  const [data, setData] = useState<SwarmLive | null>(null);
  const [pollInterval, setPollInterval] = useState(2000);
  const [expandedJobs, setExpandedJobs] = useState<Record<string, boolean>>({});
  const [expandedAlts, setExpandedAlts] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let active = true;
    const fetchSwarm = () => {
      api.swarmLive()
        .then((res) => {
          if (!active) return;
          setData(res);
          const hasRunning = (res.jobs || []).some((j) => jobStatus(j) === "in_progress");
          const nextInterval = hasRunning ? 2000 : 5000;
          if (nextInterval !== pollInterval) setPollInterval(nextInterval);
        })
        .catch((err) => {
          console.error("Swarm Live polling error:", err);
        });
    };
    fetchSwarm();
    const intervalId = setInterval(fetchSwarm, pollInterval);
    return () => {
      active = false;
      clearInterval(intervalId);
    };
  }, [pollInterval]);

  const jobs = data?.jobs || [];
  const runningCount = jobs.filter((j) => jobStatus(j) === "in_progress").length;
  const doneCount = jobs.filter((j) => jobStatus(j) === "completed").length;
  const anyRunning = runningCount > 0;

  return (
    <div className="flex flex-col h-full overflow-hidden bg-panel">
      {/* Persistent header: the tracker always announces itself, with live
          aggregate counts, so it reads as a dashboard even at rest. */}
      <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-edge/60 select-none">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-faint font-semibold">
          <Network size={11} className="text-faint/70" />
          <span>Swarm Tracker</span>
          {jobs.length > 0 && <span className="text-faint/60 normal-case tracking-normal">({jobs.length})</span>}
        </div>
        <div className="flex items-center gap-2.5 text-[10px]">
          {anyRunning && (
            <span className="flex items-center gap-1 text-accent">
              <Loader2 size={10} className="animate-spin" /> {runningCount} running
            </span>
          )}
          {doneCount > 0 && (
            <span className="flex items-center gap-1 text-good/80">
              <CheckCircle2 size={10} /> {doneCount}
            </span>
          )}
        </div>
      </div>

      {/* Scrollable Jobs list */}
      <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
        {jobs.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 text-center px-6 gap-2">
            <Network size={20} className="text-faint/50" />
            <span className="text-[12px] text-muted font-medium">No swarm jobs yet</span>
            <span className="text-[10.5px] text-faint leading-relaxed">
              Every dispatched worker lands here -- run_implement, run_parallel,
              and run_swarm alike -- with its phase, router choice, live workers,
              and streamed findings. Inline tool calls stay in the chat.
            </span>
          </div>
        ) : (
          jobs.slice().reverse().map((j) => {
            const st = jobStatus(j);
            const manualExpanded = expandedJobs[j.id];
            const isExpanded = manualExpanded !== undefined ? manualExpanded : (st === "in_progress");
            const phase = jobPhase(j);

            const artifacts = j.artifacts || [];
            const routingArts = artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() === "ROUTING");
            const streamArts = artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() !== "ROUTING");
            const tasks = j.tasks || [];
            const routerModel = routingArts.find((a: Artifact) => a.model)?.model || "";
            const workerCount = tasks.length;
            const adapter = j.adapter || tasks[0]?.adapter || "";

            return (
              <div
                key={j.id}
                className={`rounded-md border bg-panel2/30 flex flex-col overflow-hidden transition-colors ${
                  st === "in_progress" ? "border-accent/30" : st === "completed" ? "border-good/25" : st === "cancelled" ? "border-risk/25" : "border-edge"
                }`}
              >
                {/* Header Row */}
                <button
                  onClick={() => setExpandedJobs((prev) => ({ ...prev, [j.id]: !isExpanded }))}
                  className="w-full flex flex-col gap-0 p-2 hover:bg-panel2/50 text-left transition-colors select-none focus:outline-none"
                >
                  <div className="flex items-center justify-between w-full">
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <span className="shrink-0 text-faint">
                        {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      </span>
                      <span className="shrink-0">
                        {st === "in_progress" ? (
                          <Loader2 size={12} className="animate-spin text-accent" />
                        ) : st === "completed" ? (
                          <CheckCircle2 size={12} className="text-good" />
                        ) : st === "cancelled" ? (
                          <XCircle size={12} className="text-risk" />
                        ) : (
                          <Circle size={12} className="text-muted" />
                        )}
                      </span>
                      <span className="font-semibold text-[11px] text-txt truncate" title={j.goal}>
                        {j.goal}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 shrink-0 text-[10px] pl-2">
                      {j.est_cost_usd !== undefined && j.est_cost_usd > 0 && (
                        <span className="font-mono text-good">${Number(j.est_cost_usd).toFixed(4)}</span>
                      )}
                      {j.tokens !== undefined && j.tokens > 0 && (
                        <span className="text-muted font-mono">{j.tokens.toLocaleString()}t</span>
                      )}
                    </div>
                  </div>

                  {/* Model + worker count + adapter -- the "who's doing this and
                      on what" line, so the swarm's shape reads without expanding. */}
                  {(routerModel || workerCount > 0 || adapter) && (
                    <div className="flex items-center gap-1.5 pl-6 pr-1 mt-1 flex-wrap">
                      {routerModel && (
                        <span className="flex items-center gap-1 text-[9px] font-mono text-accent/90 bg-accent/10 px-1.5 py-0.5 rounded" title={`Router model: ${routerModel}`}>
                          <Cpu size={9} /> {routerModel}
                        </span>
                      )}
                      {workerCount > 0 && (
                        <span className="text-[9px] text-muted bg-panel2/60 px-1.5 py-0.5 rounded tabular-nums">
                          {workerCount} worker{workerCount > 1 ? "s" : ""}
                        </span>
                      )}
                      {adapter && (
                        <span className="text-[9px] text-faint bg-panel2/40 px-1.5 py-0.5 rounded lowercase">{adapter}</span>
                      )}
                    </div>
                  )}

                  {/* Phase strip + label -- the at-a-glance "where is this swarm". */}
                  <div className="flex items-center gap-2 pl-6 pr-1 mt-1">
                    <div className="flex-1"><PhaseStrip job={j} /></div>
                    <span className={`text-[9px] font-medium tabular-nums shrink-0 ${
                      phase.failed ? "text-risk/80" : phase.key === "done" ? "text-good/80" : "text-accent/80"
                    }`}>
                      {phase.label}
                    </span>
                  </div>
                </button>

                {/* Expanded details */}
                {isExpanded && (
                  <div className="px-2 pb-2 pt-1 flex flex-col gap-2 bg-panel2/10">
                    {/* Routing */}
                    {routingArts.length > 0 && (
                      <div className="flex flex-col gap-1.5">
                        {routingArts.map((art: Artifact, idx: number) => {
                          const hasRejected = art.rejected && art.rejected.length > 0;
                          const key = `${art.id || idx}`;
                          const altsExpanded = !!expandedAlts[key];
                          return (
                            <div key={key} className="p-2 bg-panel rounded border border-edge/45 text-[10px] flex flex-col gap-1.5">
                              <div className="flex items-center justify-between text-muted">
                                <span className="flex items-center gap-1.5 truncate max-w-[72%]">
                                  <Cpu size={11} className="text-accent shrink-0" />
                                  <span className="text-txt font-mono font-medium truncate" title={art.model}>
                                    {art.model || "Unknown model"}
                                  </span>
                                </span>
                                <span className="font-mono text-good shrink-0 font-semibold">
                                  {art.est_cost_usd !== undefined && art.est_cost_usd > 0
                                    ? `$${Number(art.est_cost_usd).toFixed(4)}`
                                    : "$0"}
                                </span>
                              </div>
                              {/* One plain-language line on why this model won. */}
                              <div className="text-[9.5px] text-faint leading-relaxed">
                                {summarizeRouting(art)}
                              </div>
                              {/* Alternatives, deliberately de-emphasized: a muted
                                  count, expanding to model-name chips (full reason on
                                  hover) instead of a red-looking wall of text. */}
                              {hasRejected && (
                                <div>
                                  <button
                                    onClick={(e) => { e.stopPropagation(); setExpandedAlts((prev) => ({ ...prev, [key]: !altsExpanded })); }}
                                    className="text-[9px] text-faint/80 hover:text-muted flex items-center gap-0.5 focus:outline-none"
                                  >
                                    {altsExpanded ? <ChevronDown size={9} /> : <ChevronRight size={9} />}
                                    {art.rejected?.length} alternatives considered
                                  </button>
                                  {altsExpanded && (
                                    <div className="mt-1.5 flex flex-wrap gap-1">
                                      {art.rejected?.map((rej: { model: string; reason: string }, ridx: number) => (
                                        <span
                                          key={ridx}
                                          title={rej.reason}
                                          className="font-mono text-[8.5px] text-faint bg-panel2/50 border border-edge/30 px-1.5 py-0.5 rounded cursor-default"
                                        >
                                          {rej.model}
                                        </span>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}

                    {/* Workers -- with a completion progress bar so a wave of
                        parallel workers reads as a single advancing unit. */}
                    {tasks.length > 0 && (
                      <div className="border-t border-edge/20 pt-1.5 flex flex-col gap-1.5">
                        <div className="flex items-center justify-between">
                          <span className="text-[9px] uppercase tracking-wider text-faint font-medium">Workers ({tasks.length})</span>
                        </div>
                        <WorkerProgress tasks={tasks} />
                        <div className="flex flex-col gap-1 mt-0.5">
                          {tasks.map((task) => {
                            const ts = taskState(task);
                            return (
                              <div key={task.id} className="p-1.5 rounded bg-panel/25 border border-edge/20 flex items-start gap-2 text-[10px]">
                                <span className="mt-0.5 shrink-0">
                                  {ts === "running" ? <Loader2 size={10} className="animate-spin text-accent" />
                                    : ts === "done" ? <CheckCircle2 size={10} className="text-good" />
                                    : ts === "fail" ? <XCircle size={10} className="text-risk" />
                                    : <Circle size={10} className="text-muted" />}
                                </span>
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center justify-between">
                                    <span className="font-semibold text-txt truncate">
                                      {task.role || "Worker"}{" "}
                                      <span className="text-faint font-normal">({task.adapter || "no-adapter"})</span>
                                    </span>
                                    <span className={`text-[8px] uppercase font-bold px-1 rounded ${
                                      ts === "running" ? "text-accent bg-accent/10"
                                        : ts === "done" ? "text-good bg-good/10"
                                        : ts === "fail" ? "text-risk bg-risk/10"
                                        : "text-muted bg-panel"
                                    }`}>{task.status}</span>
                                  </div>
                                  {task.instruction && (
                                    <div className="text-muted text-[9.5px] mt-0.5 truncate" title={task.instruction}>{task.instruction}</div>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    {/* Findings / artifacts stream -- the substance of an audit,
                        made first-class: type badge, confidence, headline. */}
                    {streamArts.length > 0 && (
                      <div className="border-t border-edge/20 pt-1.5 flex flex-col">
                        <div className="text-[9px] uppercase tracking-wider text-faint font-medium mb-1">
                          Findings ({streamArts.length})
                        </div>
                        <div className="pr-1 flex flex-col gap-1 border border-edge/20 rounded p-1.5 bg-panel/30">
                          {streamArts.map((art: Artifact, idx: number) => (
                            <div key={art.id || idx} className="text-[9.5px] border-b border-edge/10 pb-1 last:border-0 last:pb-0 flex flex-col gap-0.5">
                              <div className="flex items-center justify-between">
                                <span className="font-bold text-accent uppercase tracking-wider text-[8px]">{art.type}</span>
                                {art.confidence !== undefined && art.confidence !== null && (
                                  <span className="text-[8px] text-faint bg-edge/20 px-1 rounded">
                                    {Math.round(art.confidence * 100)}%
                                  </span>
                                )}
                              </div>
                              <div className="text-txt break-words leading-relaxed">{art.headline}</div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {tasks.length === 0 && streamArts.length === 0 && routingArts.length === 0 && (
                      <div className="text-[9.5px] text-faint italic px-1 py-0.5">
                        {st === "in_progress" ? "Worker running -- artifacts will stream in as they land." : "No artifacts recorded."}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Footer: session totals. */}
      {data?.session && (
        <div className="shrink-0 border-t border-edge bg-panel2/80 px-3 py-2 flex items-center justify-between text-[10px] text-muted font-medium select-none">
          <div className="flex items-center gap-1.5 min-w-0">
            {anyRunning ? (
              <>
                <Loader2 size={11} className="text-accent shrink-0 animate-spin" />
                <span className="truncate">SWARM RUNNING: <span className="text-txt font-mono font-semibold">{data.session.driver || "unknown"}</span></span>
              </>
            ) : (
              <>
                <Activity size={11} className="text-faint shrink-0" />
                <span className="truncate text-faint">Session total <span className="text-muted font-mono">{data.session.driver || ""}</span></span>
              </>
            )}
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <span>Cost: <strong className="text-good font-mono font-semibold">${Number(data.session.est_cost_usd).toFixed(4)}</strong></span>
            <span>Tokens: <strong className="text-txt font-mono font-semibold">{data.session.tokens_used.toLocaleString()}</strong></span>
          </div>
        </div>
      )}
    </div>
  );
}
