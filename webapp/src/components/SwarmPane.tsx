import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Loader2, CheckCircle2, XCircle, Circle, ChevronDown, ChevronRight, Cpu, Activity, Network, X } from "lucide-react";
import { api, jobArtifactList, type SwarmLive, type Job, type Artifact, type Task } from "../lib/api";

// A clean, self-contained hover tooltip. The native `title=` tooltip renders as a
// large unstyled OS box that covers the tracker and never wraps sensibly; this
// draws a width-capped, styled bubble through a portal so it escapes the pane's
// overflow clip and clamps to the viewport instead of running off the right edge.
function Tooltip({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const TIP_WIDTH = 340;
  const show = () => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const x = Math.max(8, Math.min(r.left, window.innerWidth - TIP_WIDTH - 8));
    setPos({ x, y: r.bottom + 6 });
  };
  const hide = () => setPos(null);
  return (
    <span ref={ref} className={className} onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      {children}
      {pos && label &&
        createPortal(
          <div
            style={{ position: "fixed", left: pos.x, top: pos.y, maxWidth: TIP_WIDTH, zIndex: 200 }}
            className="pointer-events-none rounded-md border border-edge bg-panel2 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-txt shadow-2xl whitespace-pre-wrap break-words"
          >
            {label}
          </div>,
          document.body,
        )}
    </span>
  );
}

type Status = "pending" | "in_progress" | "completed" | "cancelled";

// Compact "how long ago" label for a job's last activity. Accepts epoch seconds
// or an ISO string (the backend sends created_at/updated_at as either). Returns
// "" when we can't parse a timestamp so the caller can omit the affordance.
function relativeSince(ts: unknown, nowMs: number): string {
  let t: number | null = null;
  if (typeof ts === "number" && isFinite(ts)) {
    // Heuristic: seconds vs milliseconds.
    t = ts > 1e12 ? ts : ts * 1000;
  } else if (typeof ts === "string" && ts) {
    const parsed = Date.parse(ts);
    if (!isNaN(parsed)) t = parsed;
  }
  if (t === null) return "";
  const secs = Math.max(0, Math.round((nowMs - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ago`;
}

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

// A job is "finished" once it can no longer change -- completed or failed. These
// are the runs we fold away so a long session doesn't stack into a wall.
function isTerminal(j: Job): boolean {
  const st = jobStatus(j);
  return st === "completed" || st === "cancelled";
}

function taskState(t: Task): "running" | "done" | "fail" | "idle" {
  const s = (t.status || "").toLowerCase();
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "running";
  if (s.includes("complete") || s.includes("done")) return "done";
  if (s.includes("fail") || s.includes("cancel") || s.includes("error")) return "fail";
  return "idle";
}

// Dismissed job ids are VIEW state, never a deletion: the durable Puppetmaster
// store (and the PM dashboard) remain the archive, so anything hidden here is
// still recallable there. Persisted so a cleared tracker stays clean across
// reloads, soft-capped so a very long-lived install can't grow it unbounded.
const DISMISS_KEY = "swarm.dismissed.v1";

function loadDismissed(): Set<string> {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

function saveDismissed(ids: Set<string>): void {
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify([...ids].slice(-2000)));
  } catch {
    // localStorage full/unavailable -- dismissal still works for this session.
  }
}

// Cheap, render-relevant fingerprint of a live-swarm payload. During a big swarm
// the payload can be ~1MB; JSON.stringify-diffing it (or blindly setData every
// poll) re-renders the whole tree for no delta and blocks the main thread. We
// hash only the fields the UI actually draws -- job/task status, counts, tokens,
// cost -- so an unchanged poll skips the re-render entirely. Artifacts are
// append-only in a run, so their count is a sound proxy for "new finding landed".
function swarmSignature(res: SwarmLive | null): string {
  if (!res) return "";
  const parts: string[] = [];
  for (const j of res.jobs || []) {
    const tasks = j.tasks || [];
    const arts = jobArtifactList(j);
    parts.push(`${j.id}:${j.status}:${tasks.length}:${arts.length}:${j.tokens ?? 0}:${(j.est_cost_usd ?? 0).toFixed(4)}`);
    for (const t of tasks) parts.push(`${t.id}=${t.status}`);
  }
  const s = res.session;
  if (s) parts.push(`S:${s.driver ?? ""}`);
  return parts.join("|");
}

// Findings arrive one-per-worker and repeat heavily: every agentic worker emits
// a VERIFICATION artifact echoing the same task instruction, so a 5-worker swarm
// shows the identical line 5x. Collapse exact (type + headline) duplicates into a
// single row with an xN badge, and sort real signal (RISK/BUG/DECISION) above
// process noise (VERIFICATION) so substance reads first.
type FindingRow = { art: Artifact; count: number };
function dedupeFindings(arts: Artifact[]): FindingRow[] {
  const rows = new Map<string, FindingRow>();
  for (const art of arts) {
    const key = `${(art.type || "").toUpperCase()}::${(art.headline || "").trim().toLowerCase()}`;
    const hit = rows.get(key);
    if (hit) hit.count += 1;
    else rows.set(key, { art, count: 1 });
  }
  const rank = (t?: string) => {
    const u = (t || "").toUpperCase();
    if (u === "RISK" || u === "BUG") return 0;
    if (u === "DECISION" || u === "FINDING") return 1;
    if (u === "VERIFICATION") return 3;
    return 2;
  };
  return [...rows.values()].sort((a, b) => rank(a.art.type) - rank(b.art.type));
}

function jobCost(j: Job): number {
  return Number(j.est_cost_usd || 0);
}

function jobTokens(j: Job): number {
  return Number(j.tokens || 0);
}

function jobCompactTokens(j: Job): number {
  return Number(j.tool_output_tokens_saved || 0);
}

function formatCost(cost: number): string {
  return cost > 0 ? `$${cost.toFixed(4)}` : "$0";
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
  const hasRouting = jobArtifactList(j).some((a) => (a.type || "").toUpperCase() === "ROUTING");

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
  const [expandedJobs, setExpandedJobs] = useState<Record<string, boolean>>({});
  const [expandedAlts, setExpandedAlts] = useState<Record<string, boolean>>({});
  const [expandedTasks, setExpandedTasks] = useState<Record<string, boolean>>({});
  const [expandedFindings, setExpandedFindings] = useState<Record<string, boolean>>({});
  const [dismissed, setDismissed] = useState<Set<string>>(loadDismissed);
  const [finishedOpen, setFinishedOpen] = useState(false);
  // Job ids we have asked the backend to cancel. Held in local view state so the
  // row can show a subtle 'cancelling...' affordance immediately, before the next
  // poll reflects the terminal 'cancelled' status from /api/swarm/live.
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  // Bumped every second so relative "last activity" times re-render while a job
  // runs, making a live worker visibly move rather than freeze between polls.
  const [nowTick, setNowTick] = useState(() => Date.now());

  const toggleTask = (id: string) => setExpandedTasks((p) => ({ ...p, [id]: !p[id] }));
  const toggleFinding = (id: string) => setExpandedFindings((p) => ({ ...p, [id]: !p[id] }));

  useEffect(() => { saveDismissed(dismissed); }, [dismissed]);

  // Drive a 1s clock only while something is running so relative "last activity"
  // labels advance live. Stops ticking when nothing is running to avoid needless
  // re-renders.
  const hasLiveJob = (data?.jobs || []).some((j) => jobStatus(j) === "in_progress");
  useEffect(() => {
    if (!hasLiveJob) return;
    // PERF: 5s, not 1s. This tick exists only to refresh relative "3s ago /
    // 2m ago" labels, which do not need per-second precision -- and each tick
    // re-renders the whole SwarmPane (every job row + phase strip). At 1s that
    // was a steady re-render tax stacked on top of a long transcript while a
    // swarm ran. Also pause it entirely while the app is backgrounded.
    let id: number | undefined;
    const start = () => {
      if (id == null && !document.hidden) id = window.setInterval(() => setNowTick(Date.now()), 5000);
    };
    const stop = () => { if (id != null) { window.clearInterval(id); id = undefined; } };
    const onVis = () => { if (document.hidden) stop(); else start(); };
    start();
    document.addEventListener("visibilitychange", onVis);
    return () => { stop(); document.removeEventListener("visibilitychange", onVis); };
  }, [hasLiveJob]);

  // Fire-and-refetch cancel. Best-effort on the backend (a provider call in a
  // Python thread cannot be force-killed), so the row shows 'cancelling...' until
  // the next poll surfaces the terminal 'cancelled' state.
  const cancelJob = async (id: string) => {
    setCancelling((prev) => new Set(prev).add(id));
    try {
      await api.swarmCancel(id);
    } catch {
      // Swallow -- a failed cancel just means the next poll keeps showing the job
      // as running; the user can retry.
    } finally {
      try {
        const res = await api.swarmLive();
        setData(res);
      } catch {
        // Ignore; the poll loop will refetch shortly.
      }
    }
  };

  // Self-scheduling poll (not setInterval) so a new request is only ever queued
  // AFTER the previous one settles. The old fixed 2s interval fired regardless of
  // whether the last request had returned: during an active swarm the backend is
  // slow (every /swarm/live formats all artifacts and holds a worker slot), so
  // requests piled up, each grabbed a slot, saturated the server, and starved
  // every other panel's fetch -- that was the "loads in chunks / can't X out of
  // settings" jank. This loop guarantees at most one in-flight poll, pauses when
  // the window is hidden, backs off when the backend is under load, and skips the
  // re-render when nothing changed.
  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let inFlight = false;
    let lastSig = "";

    const schedule = (ms: number) => {
      if (active) timer = window.setTimeout(tick, ms);
    };

    const tick = () => {
      if (document.hidden) { schedule(3000); return; }
      if (inFlight) { schedule(500); return; }
      inFlight = true;
      const startedAt = performance.now();
      api.swarmLive()
        .then((res) => {
          if (!active) return;
          const sig = swarmSignature(res);
          if (sig !== lastSig) { lastSig = sig; setData(res); }
          const hasRunning = (res.jobs || []).some((j) => jobStatus(j) === "in_progress");
          const elapsed = performance.now() - startedAt;
          // Base cadence by activity; add backoff proportional to how slow the
          // backend just was, so we relieve rather than amplify contention.
          const base = hasRunning ? 2000 : 5000;
          const backoff = elapsed > 1500 ? Math.min(elapsed, 8000) : 0;
          schedule(base + backoff);
        })
        .catch(() => { if (active) schedule(8000); })
        .finally(() => { inFlight = false; });
    };

    tick();
    // Resume promptly when the user returns to a paused tab.
    const onVisible = () => {
      if (!document.hidden && !inFlight) { window.clearTimeout(timer); tick(); }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const allJobs = data?.jobs || [];
  const visibleJobs = allJobs.filter((j) => !dismissed.has(j.id));
  const running = visibleJobs.filter((j) => !isTerminal(j));
  const finished = visibleJobs.filter((j) => isTerminal(j));
  const failedCount = finished.filter((j) => jobStatus(j) === "cancelled").length;
  const runningCount = running.filter((j) => jobStatus(j) === "in_progress").length;
  const anyRunning = runningCount > 0;

  const dismissJob = (id: string) =>
    setDismissed((prev) => new Set(prev).add(id));
  const clearFinished = () =>
    setDismissed((prev) => {
      const next = new Set(prev);
      for (const j of finished) next.add(j.id);
      return next;
    });
  const restoreDismissed = () => setDismissed(new Set());
  const hiddenCount = allJobs.length - visibleJobs.length;

  // One card renderer, reused by both the running list and the Finished
  // accordion. Defined in-scope so it closes over the expand/dismiss state
  // instead of threading a dozen props.
  const renderJob = (j: Job) => {
    const st = jobStatus(j);
    const manualExpanded = expandedJobs[j.id];
    const isExpanded = manualExpanded !== undefined ? manualExpanded : (st === "in_progress");
    const phase = jobPhase(j);

    const artifacts = jobArtifactList(j);
    const routingArts = artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() === "ROUTING");
    const streamArts = artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() !== "ROUTING");
    const tasks = j.tasks || [];
    const routerModel = j.model || routingArts.find((a: Artifact) => a.model)?.model || "";
    const workerCount = tasks.length;
    const adapter = j.adapter || tasks[0]?.adapter || "";
    const displayModel = routerModel || adapter;
    const terminal = isTerminal(j);

    const toggle = () => setExpandedJobs((prev) => ({ ...prev, [j.id]: !isExpanded }));

    return (
      <div
        key={j.id}
        // shrink-0 is load-bearing: as a flex child of the flex-col scroll list,
        // an overflow-hidden card is allowed to shrink BELOW its content, so it
        // collapsed and clipped its own findings instead of pushing the list into
        // overflow. Pinning shrink-0 keeps the card at full content height so the
        // list actually scrolls.
        className={`shrink-0 rounded-md border bg-panel2/30 flex flex-col overflow-hidden transition-colors ${
          st === "in_progress" ? "border-accent/30" : st === "completed" ? "border-good/25" : st === "cancelled" ? "border-risk/25" : "border-edge"
        }`}
      >
        {/* Header row. A div (not a button) so the dismiss control can be a real
            nested button without invalid button-in-button markup. */}
        <div
          role="button"
          tabIndex={0}
          onClick={toggle}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}
          className="w-full flex flex-col gap-0 p-2 hover:bg-panel2/50 text-left transition-colors select-none cursor-pointer focus:outline-none"
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
              <Tooltip label={j.goal} className="font-semibold text-[11px] text-txt truncate">
                {j.goal}
              </Tooltip>
            </div>
            <div className="flex items-center gap-3 shrink-0 text-[10px] pl-2">
              {(terminal || jobCost(j) > 0 || jobTokens(j) > 0 || jobCompactTokens(j) > 0) && (
                <span className="text-muted font-mono flex items-center gap-1.5">
                  {jobTokens(j) > 0 && <span>{jobTokens(j).toLocaleString()}t</span>}
                  {jobCompactTokens(j) > 0 && (
                    <span className="text-accent/90">{jobCompactTokens(j).toLocaleString()} compact</span>
                  )}
                  <span className="text-good/90">{formatCost(jobCost(j))}</span>
                </span>
              )}
              {/* Kill: running jobs only. Best-effort cooperative cancel on the
                  backend. Shows 'cancelling...' until the next poll flips the job
                  to a terminal state. */}
              {st === "in_progress" && (
                cancelling.has(j.id) ? (
                  <span className="text-[9px] text-risk/70 italic tabular-nums">cancelling...</span>
                ) : (
                  <button
                    onClick={(e) => { e.stopPropagation(); void cancelJob(j.id); }}
                    title="Cancel this job"
                    className="text-faint/50 hover:text-risk transition-colors focus:outline-none"
                  >
                    <X size={12} />
                  </button>
                )
              )}
              {/* Dismiss: terminal runs only -- hiding a live worker would be
                  confusing. Non-destructive; the run stays in PM history. */}
              {terminal && (
                <button
                  onClick={(e) => { e.stopPropagation(); dismissJob(j.id); }}
                  title="Dismiss from tracker (stays in Puppetmaster history)"
                  className="text-faint/50 hover:text-risk transition-colors focus:outline-none"
                >
                  <X size={12} />
                </button>
              )}
            </div>
          </div>

          {/* Model + worker count + adapter -- the "who's doing this and on what"
              line, so the swarm's shape reads without expanding. */}
          {(displayModel || workerCount > 0 || adapter) && (
            <div className="flex items-center gap-1.5 pl-6 pr-1 mt-1 flex-wrap">
              {displayModel && (
                <span className="flex items-center gap-1 text-[9px] font-mono text-accent/90 bg-accent/10 px-1.5 py-0.5 rounded" title={`Model: ${displayModel}`}>
                  <Cpu size={9} /> {displayModel}
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

          {/* Live-progress line for a running job: a "last activity" relative time
              plus a compact token readout that updates on each poll, so the row
              visibly moves instead of sitting on a static spinner. Cost is
              deliberately omitted here (shown once in the bottom status bar). */}
          {st === "in_progress" && (() => {
            const since = relativeSince(j.updated_at ?? j.created_at, nowTick);
            const showTokens = j.tokens !== undefined && j.tokens > 0;
            if (!since && !showTokens) return null;
            return (
              <div className="flex items-center gap-2 pl-6 pr-1 mt-1 text-[9px] text-faint tabular-nums">
                {since && (
                  <span className="flex items-center gap-1">
                    <Activity size={9} className="text-accent/70 animate-pulse" />
                    {since}
                  </span>
                )}
                {showTokens && <span className="font-mono text-muted">{j.tokens!.toLocaleString()}t</span>}
              </div>
            );
          })()}
        </div>

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
                      {/* Alternatives, deliberately de-emphasized: a muted count,
                          expanding to model-name chips (full reason on hover)
                          instead of a red-looking wall of text. */}
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
                                <Tooltip
                                  key={ridx}
                                  label={rej.reason}
                                  className="font-mono text-[8.5px] text-faint bg-panel2/50 border border-edge/30 px-1.5 py-0.5 rounded cursor-default"
                                >
                                  {rej.model}
                                </Tooltip>
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

            {/* Single-worker / provider jobs (run_implement, run_parallel) have
                NO routing artifact, so the per-worker cost row above never
                rendered for them -- the dash showed a single-worker swarm with no
                price. Synthesize a cost/model line from the job fields so every
                job surfaces its spend, matching multi-worker swarms. */}
            {routingArts.length === 0 && ((j.est_cost_usd ?? 0) > 0 || (j.tokens ?? 0) > 0) && (
              <div className="p-2 bg-panel rounded border border-edge/45 text-[10px] flex items-center justify-between text-muted">
                <span className="flex items-center gap-1.5 truncate max-w-[72%]">
                  <Cpu size={11} className="text-accent shrink-0" />
                  <span className="text-txt font-mono font-medium truncate" title={displayModel}>
                    {displayModel || "provider worker"}
                  </span>
                </span>
                <span className="flex items-center gap-2 shrink-0 font-mono">
                  {(j.tokens ?? 0) > 0 && <span className="text-faint">{j.tokens!.toLocaleString()}t</span>}
                  <span className="text-good font-semibold">
                    {(j.est_cost_usd ?? 0) > 0 ? `$${Number(j.est_cost_usd).toFixed(4)}` : "$0"}
                  </span>
                </span>
              </div>
            )}

            {/* Workers -- with a completion progress bar so a wave of parallel
                workers reads as a single advancing unit. */}
            {tasks.length > 0 && (
              <div className="border-t border-edge/20 pt-1.5 flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[9px] uppercase tracking-wider text-faint font-medium">Workers ({tasks.length})</span>
                </div>
                <WorkerProgress tasks={tasks} />
                <div className="flex flex-col gap-1 mt-0.5">
                  {tasks.map((task) => {
                    const ts = taskState(task);
                    const tExpanded = !!expandedTasks[task.id];
                    const hasInstruction = !!task.instruction;
                    return (
                      <div
                        key={task.id}
                        role={hasInstruction ? "button" : undefined}
                        tabIndex={hasInstruction ? 0 : undefined}
                        onClick={hasInstruction ? () => toggleTask(task.id) : undefined}
                        onKeyDown={hasInstruction ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleTask(task.id); } } : undefined}
                        className={`p-1.5 rounded bg-panel/25 border border-edge/20 flex items-start gap-2 text-[10px] ${hasInstruction ? "cursor-pointer hover:bg-panel/45 focus:outline-none" : ""}`}
                      >
                        <span className="mt-0.5 shrink-0">
                          {ts === "running" ? <Loader2 size={10} className="animate-spin text-accent" />
                            : ts === "done" ? <CheckCircle2 size={10} className="text-good" />
                            : ts === "fail" ? <XCircle size={10} className="text-risk" />
                            : <Circle size={10} className="text-muted" />}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between gap-1">
                            <span className="font-semibold text-txt truncate flex items-center gap-1 min-w-0">
                              {hasInstruction && (tExpanded
                                ? <ChevronDown size={9} className="text-faint shrink-0" />
                                : <ChevronRight size={9} className="text-faint shrink-0" />)}
                              <span className="truncate">
                                {task.role || "Worker"}{" "}
                                <span className="text-faint font-normal">({task.adapter || "no-adapter"})</span>
                              </span>
                            </span>
                            <span className={`text-[8px] uppercase font-bold px-1 rounded shrink-0 ${
                              ts === "running" ? "text-accent bg-accent/10"
                                : ts === "done" ? "text-good bg-good/10"
                                : ts === "fail" ? "text-risk bg-risk/10"
                                : "text-muted bg-panel"
                            }`}>{task.status}</span>
                          </div>
                          {hasInstruction && (tExpanded ? (
                            <div className="text-muted text-[9.5px] mt-1 whitespace-pre-wrap break-words leading-relaxed">{task.instruction}</div>
                          ) : (
                            <Tooltip label={task.instruction} className="block text-muted text-[9.5px] mt-0.5 truncate">{task.instruction}</Tooltip>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Findings / artifacts stream -- the substance of an audit, made
                first-class: type badge, confidence, headline. */}
            {streamArts.length > 0 && (() => {
              const findingRows = dedupeFindings(streamArts);
              return (
              <div className="border-t border-edge/20 pt-1.5 flex flex-col">
                <div className="text-[9px] uppercase tracking-wider text-faint font-medium mb-1">
                  Findings ({findingRows.length}{findingRows.length !== streamArts.length ? ` of ${streamArts.length}` : ""})
                </div>
                <div className="pr-1 flex flex-col gap-1 border border-edge/20 rounded p-1.5 bg-panel/30">
                  {findingRows.map(({ art, count }, idx: number) => {
                    const fid = art.id || `f${idx}`;
                    const fExpanded = !!expandedFindings[fid];
                    const detailStr = art.detail == null
                      ? ""
                      : (typeof art.detail === "string"
                          ? art.detail
                          : (() => { try { return JSON.stringify(art.detail, null, 2); } catch { return String(art.detail); } })());
                    return (
                    <div key={fid} className="text-[9.5px] border-b border-edge/10 pb-1 last:border-0 last:pb-0 flex flex-col gap-0.5">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-bold text-accent uppercase tracking-wider text-[8px] flex items-center gap-1">
                          {art.type}
                          {count > 1 && <span className="text-faint bg-edge/20 px-1 rounded normal-case tracking-normal">x{count}</span>}
                        </span>
                        {art.confidence !== undefined && art.confidence !== null && (
                          <span className="text-[8px] text-faint bg-edge/20 px-1 rounded shrink-0">
                            {Math.round(art.confidence * 100)}%
                          </span>
                        )}
                      </div>
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => toggleFinding(fid)}
                        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleFinding(fid); } }}
                        className="flex items-start gap-1 text-txt break-words leading-relaxed cursor-pointer hover:text-white focus:outline-none"
                      >
                        <span className="mt-0.5 shrink-0 text-faint">
                          {fExpanded ? <ChevronDown size={9} /> : <ChevronRight size={9} />}
                        </span>
                        {fExpanded ? (
                          <span className="flex-1 min-w-0 whitespace-pre-wrap">{art.headline}</span>
                        ) : (
                          <Tooltip label={art.headline} className="flex-1 min-w-0 line-clamp-2">{art.headline}</Tooltip>
                        )}
                      </div>
                      {fExpanded && detailStr && (
                        <div className="mt-1 ml-4 text-[9px] text-muted whitespace-pre-wrap break-words bg-panel/40 border border-edge/20 rounded p-1.5 font-mono max-h-72 overflow-auto">
                          {detailStr}
                        </div>
                      )}
                    </div>
                    );
                  })}
                </div>
              </div>
              );
            })()}

            {tasks.length === 0 && streamArts.length === 0 && routingArts.length === 0 && (
              <div className="text-[9.5px] text-faint italic px-1 py-0.5">
                {st === "in_progress" ? "Worker running -- artifacts will stream in as they land." : "No artifacts recorded."}
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="flex flex-col h-full overflow-hidden bg-panel">
      {/* Persistent header: the tracker always announces itself, with live
          aggregate counts, so it reads as a dashboard even at rest. */}
      <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-edge/60 select-none">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-faint font-semibold">
          <Network size={11} className="text-faint/70" />
          <span>Swarm Tracker</span>
          {visibleJobs.length > 0 && <span className="text-faint/60 normal-case tracking-normal">({visibleJobs.length})</span>}
        </div>
        <div className="flex items-center gap-2.5 text-[10px]">
          {anyRunning && (
            <span className="flex items-center gap-1 text-accent">
              <Loader2 size={10} className="animate-spin" /> {runningCount} running
            </span>
          )}
          {finished.length > 0 && (
            <span className="flex items-center gap-1 text-good/80">
              <CheckCircle2 size={10} /> {finished.length - failedCount}
            </span>
          )}
        </div>
      </div>

      {/* Scrollable Jobs list. min-h-0 is load-bearing: without it a flex-1 item
          in a flex-col defaults to min-height:auto, refuses to shrink below its
          content, grows past the panel, and the root's overflow-hidden clips it
          -- so overflow-y-auto never engages and the list can't scroll. */}
      <div className="flex-1 min-h-0 overflow-y-auto p-2 flex flex-col gap-2">
        {visibleJobs.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 text-center px-6 gap-2">
            <Network size={20} className="text-faint/50" />
            <span className="text-[12px] text-muted font-medium">
              {hiddenCount > 0 ? "All swarm jobs cleared" : "No swarm jobs yet"}
            </span>
            {hiddenCount > 0 ? (
              // "Clear" hid every job. Without this affordance the pane read as
              // "No swarm jobs yet" even though the backend had a full history --
              // indistinguishable from a broken tracker.
              <button
                onClick={restoreDismissed}
                className="text-[10.5px] text-accent hover:underline focus:outline-none"
              >
                Show {hiddenCount} hidden job{hiddenCount === 1 ? "" : "s"}
              </button>
            ) : (
              <span className="text-[10.5px] text-faint leading-relaxed">
                Every dispatched worker lands here -- run_implement, run_parallel,
                and run_swarm alike -- with its phase, router choice, live workers,
                and streamed findings. Inline tool calls stay in the chat.
              </span>
            )}
          </div>
        ) : (
          <>
            {/* Active runs pinned on top, newest first. */}
            {running.slice().reverse().map(renderJob)}

            {/* Finished runs folded into a collapsible section so a long session
                stays a short list. Non-destructive: "Clear" only hides. */}
            {finished.length > 0 && (
              <div className="shrink-0 flex flex-col gap-2">
                <div className="flex items-center justify-between px-1 pt-0.5">
                  <button
                    onClick={() => setFinishedOpen((o) => !o)}
                    className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-faint font-semibold hover:text-muted focus:outline-none"
                  >
                    {finishedOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                    Finished
                    <span className="text-faint/60 normal-case tracking-normal">({finished.length})</span>
                    {failedCount > 0 && (
                      <span className="text-risk/70 normal-case tracking-normal">{"\u00b7"} {failedCount} failed</span>
                    )}
                  </button>
                  <button
                    onClick={clearFinished}
                    title="Hide all finished runs from the tracker (stays in Puppetmaster history)"
                    className="text-[9px] text-faint/70 hover:text-risk uppercase tracking-wider focus:outline-none"
                  >
                    Clear
                  </button>
                </div>
                {finishedOpen && finished.slice().reverse().map(renderJob)}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
