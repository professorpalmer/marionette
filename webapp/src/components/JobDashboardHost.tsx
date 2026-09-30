import { useEffect, useRef, useState } from "react";
import { ExternalLink, Maximize2, X } from "lucide-react";
import type { Job } from "../lib/api";
import { api } from "../lib/api";
import { dashboardJobId } from "../lib/jobClassification";
import { openAgentUrlExternal } from "../lib/agentLinks";
import {
  JOBS_DASHBOARD_EXPAND_MIN_PX,
  JOBS_DASHBOARD_FOCUS_MIN_PX,
  dashboardLocateError,
  dashboardUnavailableMessage,
  notifyJobsDashboardChrome,
  requestRightMinWidth,
  type DashboardLocate,
} from "../lib/jobsDashboard";
import { jobDisplayTitle } from "../lib/jobDisplayTitle";
import { lastSelectedProjectRoot } from "../lib/panelTransition";

/**
 * Host the stock Puppetmaster dashboard in the Jobs rail.
 *
 * Every hire opens the workspace board. A durable ``job_…`` deep-links via
 * ``?job=&embed=1``. Local aliases (``local-swarm-call_…``, provider workers)
 * still open the board — they are never passed as CLI job tokens.
 */
export default function JobDashboardHost({
  job,
  onClose,
}: {
  job: Job;
  onClose: () => void;
}) {
  const isDesktop = !!(window as { harnessIPC?: unknown }).harnessIPC;
  const webviewRef = useRef<HTMLElement | null>(null);
  const [locate, setLocate] = useState<DashboardLocate | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const title = jobDisplayTitle(job);
  const deepLinkId = dashboardJobId(job);
  const frameKey = deepLinkId || job.id;
  const embedUrl = locate?.embed_url || locate?.url || "";
  const showId = Boolean(deepLinkId) && deepLinkId !== title;

  useEffect(() => {
    requestRightMinWidth(JOBS_DASHBOARD_FOCUS_MIN_PX);
    notifyJobsDashboardChrome(true);
    return () => notifyJobsDashboardChrome(false);
  }, [job.id, deepLinkId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setLocate(null);
    const repo = lastSelectedProjectRoot() || undefined;
    const locateDashboard = api.dashboard;
    if (typeof locateDashboard !== "function") {
      setError(dashboardUnavailableMessage());
      setLoading(false);
      return;
    }
    // Durable job_… only for deep-link; omit otherwise so the CLI lands on the list.
    const locateArg = deepLinkId.startsWith("job_") ? deepLinkId : undefined;
    locateDashboard(locateArg, repo)
      .then((payload) => {
        if (cancelled) return;
        setLocate(payload);
        if (!payload.ok || !(payload.embed_url || payload.url)) {
          setError(dashboardLocateError(payload));
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(dashboardUnavailableMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [deepLinkId, job.id]);

  const popOut = () => {
    if (embedUrl) openAgentUrlExternal(embedUrl);
  };

  return (
    <section
      data-testid="job-dashboard-host"
      data-job-id={job.id}
      data-dashboard-job-id={frameKey}
      data-chrome="compact"
      aria-label={`Puppetmaster dashboard for ${title}`}
      className="job-dashboard-host flex flex-col h-full min-h-0 overflow-hidden text-txt"
    >
      <header className="job-dashboard-chrome" data-testid="job-dashboard-chrome">
        <div className="min-w-0 flex-1 flex items-baseline gap-1.5">
          <h2 className="truncate text-ui-11 font-semibold leading-none tracking-tight text-txt" title={title}>
            {title}
          </h2>
          {showId && (
            <span className="truncate font-mono text-ui-9 leading-none text-faint" title={deepLinkId}>
              {deepLinkId}
            </span>
          )}
        </div>
        <div className="flex items-center shrink-0">
          <button
            type="button"
            className="right-pane-icon-btn"
            aria-label="Expand"
            title="Expand"
            onClick={() => requestRightMinWidth(JOBS_DASHBOARD_EXPAND_MIN_PX)}
          >
            <Maximize2 size={12} strokeWidth={1.75} />
          </button>
          <button
            type="button"
            className="right-pane-icon-btn"
            aria-label="Pop out"
            title="Pop out"
            disabled={!embedUrl}
            onClick={popOut}
          >
            <ExternalLink size={12} strokeWidth={1.75} />
          </button>
          <button
            type="button"
            className="right-pane-icon-btn"
            aria-label="Close"
            title="Close"
            onClick={onClose}
          >
            <X size={12} strokeWidth={1.75} />
          </button>
        </div>
      </header>
      <div className="relative flex-1 min-h-0 bg-[var(--shell-chat,#0f1113)]">
        {error && (
          <p role="alert" className="px-2.5 py-2 text-ui-11 leading-snug text-risk">
            {error} The Jobs list is still available — Close to return.
          </p>
        )}
        {loading && !embedUrl && (
          <p role="status" className="px-2.5 py-2 text-ui-11 text-muted">Opening Puppetmaster dashboard…</p>
        )}
        {embedUrl && (isDesktop ? (
          <webview
            ref={webviewRef}
            src={embedUrl}
            data-testid="job-dashboard-webview"
            className="absolute inset-0 w-full h-full border-0"
            style={{ backgroundColor: "#0f1113" }}
          />
        ) : (
          <iframe
            src={embedUrl}
            title={`Puppetmaster dashboard ${frameKey}`}
            data-testid="job-dashboard-frame"
            className="absolute inset-0 w-full h-full border-0 bg-[var(--shell-chat,#0f1113)]"
          />
        ))}
      </div>
    </section>
  );
}
