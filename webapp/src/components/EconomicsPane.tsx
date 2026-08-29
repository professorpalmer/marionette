import { useEffect, useState } from "react";
import { CircleDollarSign } from "lucide-react";
import { api, type EconomicsData, type EconomicsScope, type UsageData } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";
import CostBreakdown, { usageToCostBreakdownData } from "./CostBreakdown";
import EconomicsDurable from "./EconomicsDurable";

type EconomicsPaneScope = "app_run" | Exclude<EconomicsScope, "window30">;

const SCOPES: Array<{ value: EconomicsPaneScope; label: string }> = [
  { value: "app_run", label: "This app run" },
  { value: "conversation", label: "This conversation" },
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

/** Right-pane Economics card: live process spend plus durable PM projection. */
export default function EconomicsPane() {
  const [session, setSession] = useState<UsageData["session"] | null>(
    () => readSWRCache<UsageData>("economics:usage")?.session ?? null,
  );
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<EconomicsPaneScope>("repo");
  const [periodDays, setPeriodDays] = useState<30 | null>(null);
  const [economics, setEconomics] = useState<EconomicsData | null>(
    () => readSWRCache<EconomicsData>("economics:repo:all") ?? null,
  );

  const loadUsage = () =>
    api.getUsage()
      .then((data) => {
        if (data?.session) {
          writeSWRCache("economics:usage", data);
          setSession(data.session);
          setError(null);
        }
      })
      .catch((err) => {
        console.error("Failed to load usage in EconomicsPane", err);
        setError("Couldn't load this app run's spend.");
      });

  const loadEconomics = () =>
    scope === "app_run"
      ? Promise.resolve()
      : Promise.resolve(api.getEconomics(scope, periodDays ?? "all"))
      .then((data) => {
        if (isEconomicsPayload(data) && (!data.scope || data.scope === scope)) {
          writeSWRCache(`economics:${scope}:${periodDays || "all"}`, data);
          setEconomics(data);
          return;
        }
        // getJSONSoft turns HTTP 400 into {ok:false} without `available`.
        if (data && typeof data === "object" && (data as { ok?: boolean }).ok === false) {
          setEconomics(null);
        }
      })
      .catch(() => {
        // Older harnesses omit GET /api/economics; keep CostBreakdown up.
      });

  const loadAll = () => {
    void loadUsage();
    void loadEconomics();
  };

  usePolling(loadAll, 10000);

  useEffect(() => {
    const onRefresh = () => { void loadAll(); };
    window.addEventListener("harness-usage-refresh", onRefresh);
    window.addEventListener("harness-session-changed", onRefresh);
    return () => {
      window.removeEventListener("harness-usage-refresh", onRefresh);
      window.removeEventListener("harness-session-changed", onRefresh);
    };
  }, [scope, periodDays]);

  useEffect(() => {
    void loadEconomics();
  }, [scope, periodDays]);


  if (!session && error) {
    return <p className="px-3 py-3 text-[11px] text-muted">{error}</p>;
  }
  if (!session) {
    return <p className="px-3 py-3 text-[11px] text-muted">Loading this app run…</p>;
  }
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
          onChange={(event) => setScope(event.target.value as EconomicsPaneScope)}
          aria-label="Economics ownership"
        >
          {SCOPES.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <select
          className="min-w-0 rounded border border-edge/60 bg-panel2/40 px-2 py-1.5 text-[11px] text-txt disabled:text-faint"
          value={scope === "app_run" ? "run" : periodDays === 30 ? "30" : "all"}
          onChange={(event) => setPeriodDays(event.target.value === "30" ? 30 : null)}
          aria-label="Economics period"
          disabled={scope === "app_run"}
        >
          {scope === "app_run" ? (
            <option value="run">Since launch</option>
          ) : PERIODS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        {scope === "app_run" ? (
          <CostBreakdown data={usageToCostBreakdownData(session)} />
        ) : (
          <EconomicsDurable
            data={economics}
            scope={scope}
          />
        )}
      </div>
    </div>
  );
}
