import { useEffect, useState } from "react";
import { CircleDollarSign } from "lucide-react";
import { api, type EconomicsData, type EconomicsScope, type UsageData } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import { readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";
import CostBreakdown, { usageToCostBreakdownData } from "./CostBreakdown";
import EconomicsDurable from "./EconomicsDurable";

function isEconomicsPayload(data: unknown): data is EconomicsData {
  return Boolean(data && typeof data === "object" && "available" in (data as object));
}

/** Right-pane Economics card: live process spend plus durable PM projection. */
export default function EconomicsPane() {
  const [session, setSession] = useState<UsageData["session"] | null>(
    () => readSWRCache<UsageData>("economics:usage")?.session ?? null,
  );
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<EconomicsScope>("repo");
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
    Promise.resolve(api.getEconomics(scope, periodDays ?? "all"))
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
      <div className="shrink-0 max-h-[45%] overflow-y-auto">
        <CostBreakdown data={usageToCostBreakdownData(session)} />
      </div>
      <div className="shrink-0 flex items-center px-3 py-2 border-y border-[var(--shell-panel-border)] select-none">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-faint font-semibold">
          <CircleDollarSign size={11} className="text-faint/70" />
          <span>Economics</span>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <EconomicsDurable
          data={economics}
          scope={scope}
          onScopeChange={setScope}
          periodDays={periodDays}
          onPeriodChange={setPeriodDays}
        />
      </div>
    </div>
  );
}
