import { useEffect, useState } from "react";
import { api, type Config } from "./lib/api";
import LeftRail from "./components/LeftRail";
import Conversation from "./components/Conversation";
import StatePane from "./components/StatePane";

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [artifacts, setArtifacts] = useState<{ type: string; headline: string; confidence?: number }[]>([]);
  const [jobsRefresh, setJobsRefresh] = useState(0);

  useEffect(() => { api.config().then(setConfig).catch(() => {}); }, []);

  return (
    <div className="h-full grid" style={{ gridTemplateColumns: "248px 1fr 320px" }}>
      <LeftRail jobsRefresh={jobsRefresh} />
      <Conversation
        config={config}
        onArtifacts={(a) => setArtifacts((prev) => [...a, ...prev])}
        onJobChange={() => setJobsRefresh((n) => n + 1)}
      />
      <StatePane artifacts={artifacts} />
    </div>
  );
}
