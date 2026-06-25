import { useEffect, useState } from "react";
import { api, type Config } from "../lib/api";

export default function PilotPicker({ config }: {
  config: Config | null;
}) {
  const [models, setModels] = useState<string[]>([]);
  const [current, setCurrent] = useState("");
  useEffect(() => {
    if (config) { setModels(config.models || [config.driver]); setCurrent(config.driver); }
  }, [config]);
  const swap = async (m: string) => {
    setCurrent(m);
    try { await api.swapPilot(m); } catch {}
  };
  if (!config) return null;
  return (
    <select value={current} onChange={(e) => swap(e.target.value)}
      className="bg-panel2 border border-edge rounded-md px-2 py-1 text-xs text-txt focus:outline-none focus:border-accent2">
      {models.map((m) => <option key={m} value={m}>{m}</option>)}
    </select>
  );
}
