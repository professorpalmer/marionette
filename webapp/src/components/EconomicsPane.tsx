import { useEffect, useRef, useState } from "react";
import { Coins } from "lucide-react";
import { api, type EconomicsData, type EconomicsScope } from "../lib/api";
import { activeSessionUsage, refreshProcessUsage, useProcessUsage } from "../lib/processUsage";
import { hasLedger } from "../lib/ledgerDisplay";
import LedgerBreakdown from "./LedgerBreakdown";
import { usePolling } from "../lib/usePolling";
import { readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";
import { lastSelectedProjectRoot } from "../lib/panelTransition";
import { repoPathsEqual } from "../lib/pathNormalize";
import CostBreakdown, {
  listPriceValueTotal,
  usageToCostBreakdownData,
} from "./CostBreakdown";
import EconomicsDurable from "./EconomicsDurable";

type EconomicsPaneScope = Exclude<EconomicsScope, "window30">;

const SCOPES: Array<{ value: EconomicsPaneScope; label: string }> = [
  { value: "conversation", label: "This session" },
  { value: "repo", label: "This repo" },
  { value: "all_projects", label: "All projects" },
];

const PERIODS = [
  { value: "all", label: "All time" },
  { value: "30", label: "Last 30 days" },
] as const;

function isEconomicsPayload(data: unknown): data is EconomicsData {
  return Boolean(data && typeof data === "object" && "available" in (data as object));
}

function economicsCacheKey(root: string, scope: EconomicsPaneScope, periodDays: 30 | null): string {
  return `economics:${root}:${scope}:${periodDays || "all"}`;
}

function readEconomicsCache(key: string): EconomicsData | undefined {
  return key.includes(":conversation:") ? undefined : readSWRCache<EconomicsData>(key);
}

/** Right-pane projection of canonical PM Economics reports. */
export default function EconomicsPane() {
  const pendingSelection = (window as any).__pmPendingEconomicsSelection as
    | { scope?: string; period?: string }
    | undefined;
  const opensAtSessionAll = pendingSelection?.scope === "conversation"
    && pendingSelection.period === "all";
  const [projectRoot, setProjectRoot] = useState(() => lastSelectedProjectRoot());
  const [scope, setScope] = useState<EconomicsPaneScope>(
    opensAtSessionAll ? "conversation" : "repo",
  );
  const [periodDays, setPeriodDays] = useState<30 | null>(null);
  const [economics, setEconomics] = useState<EconomicsData | null>(
    () => readEconomicsCache(
      economicsCacheKey(
        lastSelectedProjectRoot(),
        opensAtSessionAll ? "conversation" : "repo",
        null,
      ),
    ) ?? null,
  );
  /** The latest read failed or answered for another repo/scope. */
  const [loadFailed, setLoadFailed] = useState(false);
  /** The shown report belongs to the previous session until the next read lands. */
  const [sessionStale, setSessionStale] = useState(false);
  const economicsRequest = useRef(0);
  const processUsage = useProcessUsage();

  const loadEconomics = (
    requestedScope = scope,
    requestedPeriod = periodDays,
    requestedRoot = projectRoot,
  ) => {
    const request = ++economicsRequest.current;
    return Promise.resolve(api.getEconomics(requestedScope, requestedPeriod ?? "all"))
      .then((data) => {
        if (request !== economicsRequest.current) return;
        if (
          isEconomicsPayload(data)
          && (!data.scope || data.scope === requestedScope)
          && (!requestedRoot || (data.repo && repoPathsEqual(data.repo, requestedRoot)))
        ) {
          writeSWRCache(
            economicsCacheKey(requestedRoot, requestedScope, requestedPeriod),
            data,
          );
          setEconomics(data);
          setLoadFailed(false);
          setSessionStale(false);
          return;
        }
        setLoadFailed(true);
      })
      .catch(() => {
        if (request === economicsRequest.current) setLoadFailed(true);
      });
  };

  const showCachedOrPrevious = (root: string, nextScope: EconomicsPaneScope, nextPeriod: 30 | null) => {
    setLoadFailed(false);
    const cached = readEconomicsCache(economicsCacheKey(root, nextScope, nextPeriod));
    if (cached) setEconomics(cached);
  };

  usePolling(loadEconomics, 10000, { enabled: Boolean(projectRoot) });

  useEffect(() => {
    const onUsageRefresh = () => {
      void loadEconomics();
    };
    const onSessionChanged = () => {
      if (scope === "conversation") {
        setSessionStale(true);
        void loadEconomics();
      }
    };
    window.addEventListener("harness-usage-refresh", onUsageRefresh);
    window.addEventListener("harness-session-changed", onSessionChanged);
    return () => {
      window.removeEventListener("harness-usage-refresh", onUsageRefresh);
      window.removeEventListener("harness-session-changed", onSessionChanged);
    };
  }, [scope, periodDays, projectRoot]);

  useEffect(() => {
    const onProject = (event: Event) => {
      const root = String((event as CustomEvent<string>).detail || "");
      if (projectRoot && root && repoPathsEqual(projectRoot, root)) return;
      economicsRequest.current += 1;
      setLoadFailed(false);
      setProjectRoot(root);
      setEconomics(
        readEconomicsCache(economicsCacheKey(root, scope, periodDays)) ?? null,
      );
      if (projectRoot) void loadEconomics(scope, periodDays, root);
    };
    window.addEventListener("harness-project-selected", onProject);
    return () => window.removeEventListener("harness-project-selected", onProject);
  }, [scope, periodDays, projectRoot]);

  useEffect(() => {
    const onSelection = (event: Event) => {
      const detail = (event as CustomEvent<{ scope?: string; period?: string }>).detail;
      if (detail?.scope !== "conversation" || detail.period !== "all") return;
      delete (window as any).__pmPendingEconomicsSelection;
      setScope("conversation");
      setPeriodDays(null);
      showCachedOrPrevious(projectRoot, "conversation", null);
      void loadEconomics("conversation", null, projectRoot);
    };
    window.addEventListener("harness-economics-selection", onSelection);
    if (opensAtSessionAll) delete (window as any).__pmPendingEconomicsSelection;
    return () => window.removeEventListener("harness-economics-selection", onSelection);
  }, [opensAtSessionAll, projectRoot]);

  const economicsMatchesSelection = Boolean(
    economics
    && (!economics.scope || economics.scope === scope)
    && (!projectRoot || (economics.repo && repoPathsEqual(economics.repo, projectRoot)))
    && (periodDays === 30 ? economics.window_days === 30 : !economics.window_days),
  );
  const reportCurrent = economicsMatchesSelection && !sessionStale;
  const projectLabel = projectRoot.split(/[\\/]/).filter(Boolean).at(-1) || "this repo";
  const sessionAllTime = scope === 'conversation' && periodDays === null;
  const sessionUsage = activeSessionUsage(processUsage);
  const processMeters = sessionAllTime && sessionUsage
    ? usageToCostBreakdownData(sessionUsage)
    : null;
  const showProcessMeters = Boolean(
    processMeters
    && (
      processMeters.read_status === "unavailable"
      || (processMeters.tokens_used ?? 0) > 0
      || (processMeters.est_cost_usd ?? 0) > 0
      || listPriceValueTotal(processMeters) > 0
    ),
  );
  return (
    <div className="flex flex-col h-full overflow-hidden bg-transparent">
      <div className="shrink-0 flex items-center px-3 py-2 border-b border-[var(--shell-panel-border)] select-none">
        <div className="flex items-center gap-1.5 text-[10px] font-medium text-muted">
          <Coins size={11} className="text-faint" />
          <span>Economics</span>
        </div>
      </div>
      <div className="shrink-0 grid grid-cols-[minmax(0,1fr)_110px] gap-2 px-3 pt-3 pb-2">
        <select
          className="min-w-0 rounded border border-edge/60 bg-panel2/40 px-2 py-1.5 text-[11px] text-txt"
          value={scope}
          onChange={(event) => {
            const nextScope = event.target.value as EconomicsPaneScope;
            setScope(nextScope);
            showCachedOrPrevious(projectRoot, nextScope, periodDays);
            void loadEconomics(nextScope, periodDays, projectRoot);
          }}
          aria-label="Economics ownership"
        >
          {SCOPES.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <select
          className="min-w-0 rounded border border-edge/60 bg-panel2/40 px-2 py-1.5 text-[11px] text-txt disabled:text-faint"
          value={periodDays === 30 ? "30" : "all"}
          onChange={(event) => {
            const nextPeriod = event.target.value === "30" ? 30 : null;
            setPeriodDays(nextPeriod);
            showCachedOrPrevious(projectRoot, scope, nextPeriod);
            void loadEconomics(scope, nextPeriod, projectRoot);
          }}
          aria-label="Economics period"
        >
          {PERIODS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        {sessionAllTime && processUsage.status === "loading" && (
          <p className="px-3 py-2 text-[11px] text-faint" role="status">Loading usage…</p>
        )}
        {sessionAllTime && processUsage.status === "unavailable" && (
          <button type="button" className="px-3 py-2 text-[11px] text-muted hover:text-txt" onClick={() => void refreshProcessUsage({ manual: true })}>
            Session usage is incomplete or unavailable. Retry
          </button>
        )}
        {sessionAllTime && hasLedger(processUsage.ledger) ? (
          <LedgerBreakdown view={processUsage.ledger} />
        ) : showProcessMeters && processMeters ? (
          <CostBreakdown data={processMeters} />
        ) : null}
        {!reportCurrent && (
          !projectRoot ? (
            <p className="px-3 py-3 text-[11px] text-muted">Select a project to see its economics.</p>
          ) : loadFailed ? (
            <button
              type="button"
              className="px-3 py-3 text-left text-[11px] text-muted hover:text-txt"
              onClick={() => {
                setLoadFailed(false);
                void loadEconomics();
              }}
            >
              Couldn't load economics for {projectLabel}. Retry
            </button>
          ) : (
            <p className="px-3 py-3 text-[11px] text-muted" role="status">Updating {projectLabel}…</p>
          )
        )}
        {economics && (reportCurrent || !loadFailed) && (
          <div
            data-testid="economics-report"
            className={reportCurrent ? undefined : "opacity-50"}
            aria-busy={!reportCurrent}
          >
            <EconomicsDurable
              data={economics}
              hero={!sessionAllTime}
            />
          </div>
        )}
      </div>
    </div>
  );
}
