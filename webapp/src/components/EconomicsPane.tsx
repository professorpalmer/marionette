import { useEffect, useState } from "react";
import { api, type EconomicsData, type EconomicsScope, type UsageData } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import CostBreakdown, { usageToCostBreakdownData } from "./CostBreakdown";
import EconomicsDurable from "./EconomicsDurable";

function isEconomicsPayload(data: unknown): data is EconomicsData {
  return Boolean(data && typeof data === "object" && "available" in (data as object));
}

/** Right-pane Economics card: live process spend plus durable PM projection. */
export default function EconomicsPane() {
  const [session, setSession] = useState<UsageData["session"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<EconomicsScope>("repo");
  const [economics, setEconomics] = useState<EconomicsData | null>(null);

  const loadUsage = () =>
    api.getUsage()
      .then((data) => {
        if (data?.session) {
          setSession(data.session);
          setError(null);
        }
      })
      .catch((err) => {
        console.error("Failed to load usage in EconomicsPane", err);
        setError("Couldn't load this app run's spend.");
      });

  const loadEconomics = () =>
    Promise.resolve(api.getEconomics(scope))
      .then((data) => {
        if (isEconomicsPayload(data) && (!data.scope || data.scope === scope)) {
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
  }, [scope]);

  useEffect(() => {
    void loadEconomics();
  }, [scope]);

  if (!session && error) {
    return <p className="px-3 py-3 text-[11px] text-muted">{error}</p>;
  }
  if (!session) {
    return <p className="px-3 py-3 text-[11px] text-muted">Loading this app run…</p>;
  }
  return (
    <div className="w-full min-h-0 overflow-auto">
      <CostBreakdown data={usageToCostBreakdownData(session)} />
      <EconomicsDurable data={economics} scope={scope} onScopeChange={setScope} />
    </div>
  );
}
