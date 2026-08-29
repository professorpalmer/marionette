import { useEffect, useRef, useState } from "react";
import { CircleDollarSign } from "lucide-react";
import { api, type EconomicsData, type EconomicsScope } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";
import { lastSelectedProjectRoot } from "../lib/panelTransition";
import { repoPathsEqual } from "../lib/pathNormalize";
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

/** Right-pane Economics card: live process spend plus durable PM projection. */
export default function EconomicsPane() {
  const [projectRoot, setProjectRoot] = useState(() => lastSelectedProjectRoot());
  const [scope, setScope] = useState<EconomicsPaneScope>("repo");
  const [periodDays, setPeriodDays] = useState<30 | null>(null);
  const [economics, setEconomics] = useState<EconomicsData | null>(
    () => readSWRCache<EconomicsData>(
      economicsCacheKey(lastSelectedProjectRoot(), "repo", null),
    ) ?? null,
  );
  const economicsRequest = useRef(0);

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
          return;
        }
        // getJSONSoft turns HTTP 400 into {ok:false} without `available`.
        if (data && typeof data === "object" && (data as { ok?: boolean }).ok === false) {
          setEconomics(null);
        }
      })
      .catch(() => {
        // Older harnesses can omit GET /api/economics.
      });
  };

  usePolling(loadEconomics, 10000, { enabled: Boolean(projectRoot) });

  useEffect(() => {
    const onUsageRefresh = () => { void loadEconomics(); };
    const onSessionChanged = () => {
      if (scope === "conversation") void loadEconomics();
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
      setProjectRoot(root);
      setEconomics(
        readSWRCache<EconomicsData>(economicsCacheKey(root, scope, periodDays)) ?? null,
      );
      if (projectRoot) void loadEconomics(scope, periodDays, root);
    };
    window.addEventListener("harness-project-selected", onProject);
    return () => window.removeEventListener("harness-project-selected", onProject);
  }, [scope, periodDays, projectRoot]);

  const economicsMatchesSelection = Boolean(
    economics
    && (!economics.scope || economics.scope === scope)
    && (!projectRoot || (economics.repo && repoPathsEqual(economics.repo, projectRoot)))
    && (periodDays === 30 ? economics.window_days === 30 : !economics.window_days),
  );
  const projectLabel = projectRoot.split(/[\\/]/).filter(Boolean).at(-1) || "this repo";
  return (
    <div className="flex flex-col h-full overflow-hidden bg-transparent">
      <div className="shrink-0 flex items-center px-3 py-2 border-b border-[var(--shell-panel-border)] select-none">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-faint font-semibold">
          <CircleDollarSign size={11} className="text-faint/70" />
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
            setEconomics(
              readSWRCache<EconomicsData>(economicsCacheKey(projectRoot, nextScope, periodDays)) ?? null,
            );
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
            setEconomics(
              readSWRCache<EconomicsData>(economicsCacheKey(projectRoot, scope, nextPeriod)) ?? null,
            );
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
        {economicsMatchesSelection ? (
          <EconomicsDurable
            data={economics}
          />
        ) : (
          <p className="px-3 py-3 text-[11px] text-muted">Updating {projectLabel}…</p>
        )}
      </div>
    </div>
  );
}
