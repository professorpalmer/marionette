import { useEffect, useState } from "react";
import { api, type UsageData } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import CostBreakdown, { usageToCostBreakdownData } from "./CostBreakdown";

/** Right-pane Economics card: live process / this-app-run spend and list-price value. */
export default function EconomicsPane() {
  const [session, setSession] = useState<UsageData["session"] | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  usePolling(loadUsage, 10000);

  useEffect(() => {
    const onRefresh = () => { void loadUsage(); };
    window.addEventListener("harness-usage-refresh", onRefresh);
    window.addEventListener("harness-session-changed", onRefresh);
    return () => {
      window.removeEventListener("harness-usage-refresh", onRefresh);
      window.removeEventListener("harness-session-changed", onRefresh);
    };
  }, []);

  if (!session && error) {
    return <p className="px-3 py-3 text-[11px] text-muted">{error}</p>;
  }
  if (!session) {
    return <p className="px-3 py-3 text-[11px] text-muted">Loading this app run…</p>;
  }
  return <CostBreakdown data={usageToCostBreakdownData(session)} />;
}
