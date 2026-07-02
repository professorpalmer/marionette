import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { api, type CodegraphStatus, type WikiGraphData } from "../lib/api";

export default function StatePane({ artifacts }: {
  artifacts: { type: string; headline: string; confidence?: number; id?: string; created_by?: string; [key: string]: any }[];
  embedded?: boolean;
}) {
  // CodeGraph status state
  const [cg, setCg] = useState<CodegraphStatus | null>(null);
  const [reindexing, setReindexing] = useState(false);

  // Wiki status state
  const [wiki, setWiki] = useState<WikiGraphData | null>(null);
  const [loadingWiki, setLoadingWiki] = useState(false);

  // Telemetry (CodeGraph / Wiki) is collapsed by default so it reads as a quiet
  // status line, not a wall of stats competing with the actual findings. The
  // full metrics are one click away. Preference persists per user.
  const [cgOpen, setCgOpen] = useState(() => localStorage.getItem("pmharness.statePane.cgOpen") === "1");
  const [wikiOpen, setWikiOpen] = useState(() => localStorage.getItem("pmharness.statePane.wikiOpen") === "1");
  const toggleCg = () => setCgOpen((v) => { localStorage.setItem("pmharness.statePane.cgOpen", v ? "0" : "1"); return !v; });
  const toggleWiki = () => setWikiOpen((v) => { localStorage.setItem("pmharness.statePane.wikiOpen", v ? "0" : "1"); return !v; });

  const fetchCg = async () => {
    try {
      const res = await api.getCodegraph();
      setCg(res);
    } catch (err) {
      console.error(err);
    }
  };

  const fetchWiki = async () => {
    setLoadingWiki(true);
    try {
      const res = await api.getWikiGraph();
      setWiki(res);
    } catch (err) {
      console.error(err);
    } finally {
      setLoadingWiki(false);
    }
  };

  useEffect(() => {
    fetchCg();
    fetchWiki();
    // Re-fetch CodeGraph + Wiki when the active project/workspace changes, so
    // the panel reflects the newly-opened repo instead of showing stale stats.
    // (Backend re-points + re-indexes CodeGraph on /api/workspace/open; the
    // panel must re-poll to see it.) Mirrors FileTree's refresh wiring.
    const onChange = () => { fetchCg(); fetchWiki(); };
    window.addEventListener("harness-config-changed", onChange);
    window.addEventListener("harness-new-session", onChange);
    return () => {
      window.removeEventListener("harness-config-changed", onChange);
      window.removeEventListener("harness-new-session", onChange);
    };
  }, []);

  // Poll /api/codegraph frequently while indexing so the panel flips to READY
  // promptly on its own when the index finishes. setInterval waits one full
  // interval before its first tick, so a slow (10s) poll left the panel showing
  // INDEXING for up to 10s after completion -- which read as "stuck until you
  // click the dir" (a manual click fires harness-config-changed -> instant
  // refetch). A tight 2s poll makes the transition feel immediate.
  useEffect(() => {
    let timer: any = null;
    if (cg?.status === "indexing") {
      timer = setInterval(() => {
        fetchCg();
      }, 2000);
    }
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [cg?.status]);

  const handleReindex = async () => {
    setReindexing(true);
    try {
      await api.reindexCodegraph();
      await fetchCg();
    } catch (err) {
      console.error(err);
    } finally {
      setReindexing(false);
    }
  };

  // Group and Dedupe Logic. Signal groups (what the user asked the swarm for)
  // stay expanded and prominent; plumbing groups (internal telemetry the pilot
  // emits along the way) default collapsed and de-emphasized, matching the quiet
  // CodeGraph/Wiki strip above -- so the eye lands on findings, not machinery.
  const PLUMBING_GROUPS = new Set(["VERIFICATION", "ROUTING", "MCP"]);
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});

  // Effective collapse: an explicit user toggle wins; otherwise plumbing starts
  // collapsed and signal starts open.
  const isGroupCollapsed = (groupName: string) =>
    collapsedGroups[groupName] !== undefined
      ? collapsedGroups[groupName]
      : PLUMBING_GROUPS.has(groupName);

  const toggleGroup = (groupName: string) => {
    setCollapsedGroups((prev) => ({
      ...prev,
      [groupName]: !isGroupCollapsed(groupName),
    }));
  };

  // The whole artifacts section collapses to a one-line header, like the
  // telemetry pills. Preference persists per user.
  const [artifactsOpen, setArtifactsOpen] = useState(
    () => localStorage.getItem("pmharness.statePane.artifactsOpen") !== "0",
  );
  const toggleArtifacts = () =>
    setArtifactsOpen((v) => {
      localStorage.setItem("pmharness.statePane.artifactsOpen", v ? "0" : "1");
      return !v;
    });

  // Signal first, plumbing last.
  const GROUP_ORDER = ["FINDING", "DECISION", "RISK", "VERIFICATION", "ROUTING", "MCP"];
  const getGroupIndex = (type: string) => {
    const idx = GROUP_ORDER.indexOf(type.toUpperCase());
    return idx === -1 ? 999 : idx;
  };

  // 1. Deduplicate artifacts by type + normalized headline
  interface ProcessedArtifact {
    type: string;
    headline: string;
    confidence: number;
    count: number;
    originalCasingHeadline: string;
    hasHeadline: boolean;
  }

  const processed: ProcessedArtifact[] = [];
  const seenKeys = new Map<string, number>();

  for (const a of artifacts) {
    const typeUpper = (a.type || "").toUpperCase();
    const trimmedHeadline = (a.headline || "").trim();
    const hasHeadline = trimmedHeadline.length > 0;
    const key = `${typeUpper}:${trimmedHeadline.toLowerCase()}`;
    const confidenceVal = a.confidence ?? 0;

    if (seenKeys.has(key)) {
      const idx = seenKeys.get(key)!;
      processed[idx].count += 1;
      if (confidenceVal > processed[idx].confidence) {
        processed[idx].confidence = confidenceVal;
      }
    } else {
      seenKeys.set(key, processed.length);
      processed.push({
        type: typeUpper,
        headline: trimmedHeadline,
        confidence: confidenceVal,
        count: 1,
        originalCasingHeadline: a.headline || "",
        hasHeadline,
      });
    }
  }

  // 2. Group by type
  const groupsMap = new Map<string, ProcessedArtifact[]>();
  for (const item of processed) {
    const grp = item.type;
    if (!groupsMap.has(grp)) {
      groupsMap.set(grp, []);
    }
    groupsMap.get(grp)!.push(item);
  }

  // Sort items inside group by confidence DESC
  for (const items of groupsMap.values()) {
    items.sort((a, b) => b.confidence - a.confidence);
  }

  // Sort group names
  const sortedGroupNames = Array.from(groupsMap.keys()).sort((a, b) => {
    const idxA = getGroupIndex(a);
    const idxB = getGroupIndex(b);
    if (idxA !== idxB) {
      return idxA - idxB;
    }
    return a.localeCompare(b);
  });

  const cgReady = cg?.status === "ready";
  const cgIndexing = cg?.status === "indexing";
  const cgDot = cgReady ? "bg-good" : cgIndexing ? "bg-accent" : "bg-faint";
  const cgWord = cgIndexing ? "indexing" : cgReady ? "ready" : cg?.status === "unsupported" ? "unsupported" : "none";
  const cgMetric = cgReady && cg?.nodes != null
    ? `${cg.nodes.toLocaleString()} nodes`
    : cgIndexing ? "working" : cg?.status === "none" ? "no workspace" : "";

  const wikiOk = wiki?.status === "ok";
  const wikiErr = wiki?.status === "error";
  const wikiDot = wikiOk ? "bg-good" : wikiErr ? "bg-risk" : "bg-faint";
  const wikiWord = wikiOk ? "connected" : wikiErr ? "error" : "off";
  const wikiMetric = wikiOk ? `${(wiki?.nodes || []).length} pages` : "";

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Telemetry status strip: CodeGraph + Wiki collapsed to quiet one-line
          pills. This is proof-of-power chrome -- kept available, kept subdued,
          so the eye lands on the findings below instead of a wall of stats. */}
      <div className="px-2 pt-2 pb-1.5 shrink-0 flex flex-col gap-1">
        {/* CodeGraph pill */}
        <div className="rounded-md border border-edge/40 bg-panel/40 overflow-hidden">
          <button
            onClick={toggleCg}
            className="w-full flex items-center gap-2 px-2.5 py-1.5 text-[10px] hover:bg-panel2/30 transition-colors"
            title={cgOpen ? "Hide CodeGraph details" : "Show CodeGraph details"}
          >
            {cgOpen ? <ChevronDown className="w-3 h-3 text-faint shrink-0" /> : <ChevronRight className="w-3 h-3 text-faint shrink-0" />}
            <span className="uppercase tracking-wider font-semibold text-faint">CodeGraph</span>
            {cgIndexing
              ? <Loader2 className="w-2.5 h-2.5 animate-spin text-accent shrink-0" />
              : <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${cgDot}`} aria-hidden />}
            <span className="text-muted lowercase">{cgWord}</span>
            <span className="flex-1" />
            {cgMetric && <span className="text-faint tabular-nums truncate">{cgMetric}</span>}
          </button>

          {cgOpen && cg?.status !== "none" && (
            <div className="px-2.5 pb-2 pt-1 border-t border-edge/30">
              <div className="grid grid-cols-3 gap-2 text-[11px]">
                <div>
                  <div className="text-faint text-[9px] uppercase tracking-wide">Nodes</div>
                  <div className="font-semibold text-muted text-[11px] mt-0.5 tabular-nums">
                    {cg?.nodes != null ? cg.nodes.toLocaleString() : "-"}
                  </div>
                </div>
                <div>
                  <div className="text-faint text-[9px] uppercase tracking-wide">Edges</div>
                  <div className="font-semibold text-muted text-[11px] mt-0.5 tabular-nums">
                    {cg?.edges != null ? cg.edges.toLocaleString() : "-"}
                  </div>
                </div>
                <div>
                  <div className="text-faint text-[9px] uppercase tracking-wide">Files</div>
                  <div className="font-semibold text-muted text-[11px] mt-0.5 tabular-nums">
                    {cg?.files != null ? cg.files.toLocaleString() : "-"}
                  </div>
                </div>
              </div>

              {cg?.languages && cg.languages.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {cg.languages.map((l) => (
                    <span key={l} className="bg-panel2 px-1 py-0.2 rounded border border-edge/60 text-[9px] text-faint">
                      {l}
                    </span>
                  ))}
                </div>
              )}

              <div className="mt-2 flex items-center justify-between gap-2">
                {cg?.last_indexed
                  ? <span className="text-[8px] text-faint truncate">Indexed {new Date(cg.last_indexed).toLocaleString()}</span>
                  : <span />}
                <button
                  onClick={handleReindex}
                  disabled={reindexing || cgIndexing}
                  className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2 shrink-0"
                >
                  {reindexing || cgIndexing ? "Indexing..." : "Re-index"}
                </button>
              </div>

              {cgIndexing && cg?.reason && (
                <div className="mt-1.5 text-[9px] text-accent/80">{cg.reason}</div>
              )}
            </div>
          )}
        </div>

        {/* Wiki pill */}
        <div className="rounded-md border border-edge/40 bg-panel/40 overflow-hidden">
          <button
            onClick={toggleWiki}
            className="w-full flex items-center gap-2 px-2.5 py-1.5 text-[10px] hover:bg-panel2/30 transition-colors"
            title={wikiOpen ? "Hide Wiki details" : "Show Wiki details"}
          >
            {wikiOpen ? <ChevronDown className="w-3 h-3 text-faint shrink-0" /> : <ChevronRight className="w-3 h-3 text-faint shrink-0" />}
            <span className="uppercase tracking-wider font-semibold text-faint">Wiki</span>
            {loadingWiki
              ? <Loader2 className="w-2.5 h-2.5 animate-spin text-faint shrink-0" />
              : <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${wikiDot}`} aria-hidden />}
            <span className="text-muted lowercase">{wikiWord}</span>
            <span className="flex-1" />
            {wikiMetric && <span className="text-faint tabular-nums truncate">{wikiMetric}</span>}
          </button>

          {wikiOpen && (
            <div className="px-2.5 pb-2 pt-1.5 border-t border-edge/30 text-[10px]">
              {wikiErr ? (
                <div className="text-risk italic">{wiki?.error || "Failed to fetch wiki status"}</div>
              ) : !wikiOk ? (
                <div className="text-faint italic">Wiki not connected (optional)</div>
              ) : (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted">
                    <strong className="text-txt tabular-nums">{(wiki?.nodes || []).length}</strong> pages,{" "}
                    <strong className="text-txt tabular-nums">{(wiki?.edges || []).length}</strong> links
                    {wiki?.base_url && <span className="text-faint ml-1.5 truncate">{wiki.base_url}</span>}
                  </span>
                  <button
                    onClick={fetchWiki}
                    disabled={loadingWiki}
                    className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2 flex items-center justify-center shrink-0"
                    title="Refresh Wiki Stats"
                  >
                    <RefreshCw className={`w-2.5 h-2.5 ${loadingWiki ? "animate-spin" : ""}`} />
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Artifacts: the hero of this tab, but collapsible to a one-line header
          so it can get out of the way like the telemetry strip above. */}
      <button
        onClick={toggleArtifacts}
        className="text-[10px] px-3 pt-1.5 pb-1 flex justify-between items-center shrink-0 border-t border-edge/30 hover:bg-panel2/20 transition-colors"
        title={artifactsOpen ? "Hide artifacts" : "Show artifacts"}
      >
        <span className="font-semibold uppercase tracking-wider text-muted flex items-center gap-1">
          {artifactsOpen ? <ChevronDown className="w-3 h-3 text-faint" /> : <ChevronRight className="w-3 h-3 text-faint" />}
          Artifacts <span className="text-faint">({artifacts.length})</span>
        </span>
      </button>

      {/* Artifacts Pane */}
      {artifactsOpen && (
      <div className="flex-1 overflow-y-auto px-2 pb-2 flex flex-col gap-1.5">
        {artifacts.length === 0 && (
          <div className="text-[11px] text-muted italic px-2 py-1">Findings appear here as the pilot investigates.</div>
        )}

        {sortedGroupNames.map((groupName) => {
          const items = groupsMap.get(groupName) || [];
          const isCollapsed = isGroupCollapsed(groupName);
          const isPlumbing = PLUMBING_GROUPS.has(groupName);
          const count = items.reduce((acc, it) => acc + it.count, 0);

          return (
            <div key={groupName} className="mb-1.5">
              {/* Group Header. Plumbing groups read fainter than signal so the
                  eye stays on findings/decisions/risks. */}
              <button
                onClick={() => toggleGroup(groupName)}
                className={`w-full flex items-center justify-between text-[10px] font-semibold py-1 px-1.5 border rounded mb-1 select-none transition-colors ${
                  isPlumbing
                    ? "text-faint hover:text-muted bg-panel/20 border-edge/20"
                    : "text-muted hover:text-txt bg-panel/40 border-edge/30"
                }`}
              >
                <span className="flex items-center gap-1">
                  {isCollapsed ? <ChevronRight className="w-3 h-3 text-faint" /> : <ChevronDown className="w-3 h-3 text-faint" />}
                  <span className="uppercase tracking-wider">{groupName}</span>
                  <span className="text-[9px] text-faint px-1 bg-edge/40 rounded-full border border-edge font-normal ml-1">
                    {count}
                  </span>
                </span>
              </button>

              {/* Group Content */}
              {!isCollapsed && (
                <div className="flex flex-col gap-1 px-0.5">
                  {items.map((item, idx) => {
                    const hasHeadline = item.hasHeadline;
                    const displayHeadline = hasHeadline
                      ? item.originalCasingHeadline
                      : `${item.type.toLowerCase()} decision`;

                    const hasConfidence = item.confidence > 0;
                    const borderHighlightClass = item.confidence >= 0.8
                      ? "border-accent/40 shadow-sm shadow-accent/5"
                      : "border-edge";

                    if (!hasHeadline) {
                      // Compact chip render
                      return (
                        <div
                          key={idx}
                          className={`flex items-center justify-between bg-panel border ${borderHighlightClass} rounded px-2 py-1 text-[11px]`}
                        >
                          <div className="flex items-center gap-1.5 truncate">
                            <span className="text-[8px] uppercase tracking-wider text-accent bg-accent2 px-1 py-0.2 rounded border border-accent/10 font-bold">
                              {item.type}
                            </span>
                            <span className="text-muted italic truncate text-[11px]">{displayHeadline}</span>
                          </div>
                          <div className="flex items-center gap-1.5 shrink-0 ml-2">
                            {hasConfidence && (
                              <span className="text-[9px] font-mono text-faint">
                                c:{item.confidence.toFixed(2)}
                              </span>
                            )}
                            {item.count > 1 && (
                              <span className="text-[9px] font-bold text-accent px-1 py-0.2 rounded-full bg-accent2 border border-accent/20">
                                x{item.count}
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    }

                    // Normal card render
                    return (
                      <div
                        key={idx}
                        className={`bg-panel2 border ${borderHighlightClass} rounded-lg p-2.5 transition-all`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="text-[12px] text-txt leading-relaxed break-words flex-1">
                            {displayHeadline}
                          </div>
                          <div className="flex items-center gap-1.5 shrink-0">
                            {hasConfidence && (
                              <span className="text-[9px] font-mono text-muted bg-edge px-1 rounded border border-edge2">
                                {item.confidence.toFixed(2)}
                              </span>
                            )}
                            {item.count > 1 && (
                              <span className="text-[9px] font-bold text-accent px-1.5 py-0.2 rounded bg-accent2 border border-accent/20">
                                x{item.count}
                              </span>
                            )}
                          </div>
                        </div>

                        {hasConfidence && (
                          <div className="mt-1.5 w-full bg-edge h-0.5 rounded-full overflow-hidden">
                            <div
                              className={`h-full ${item.confidence >= 0.8 ? "bg-good" : "bg-accent"}`}
                              style={{ width: `${item.confidence * 100}%` }}
                            />
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
      )}
    </div>
  );
}
