import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { api, type CodegraphStatus } from "../lib/api";

export default function StatePane({ artifacts }: {
  artifacts: { type: string; headline: string; confidence?: number; id?: string; created_by?: string; [key: string]: any }[];
  embedded?: boolean;
}) {
  // CodeGraph status state
  const [cg, setCg] = useState<CodegraphStatus | null>(null);
  const [reindexing, setReindexing] = useState(false);

  const fetchCg = async () => {
    try {
      const res = await api.getCodegraph();
      setCg(res);
    } catch (err) {
      console.error(err);
    }
  };

  useEffect(() => {
    fetchCg();
  }, []);

  // Poll /api/codegraph every 10s while indexing
  useEffect(() => {
    let timer: any = null;
    if (cg?.status === "indexing") {
      timer = setInterval(() => {
        fetchCg();
      }, 10000);
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

  // Group and Dedupe Logic
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});

  const toggleGroup = (groupName: string) => {
    setCollapsedGroups((prev) => ({
      ...prev,
      [groupName]: !prev[groupName],
    }));
  };

  const GROUP_ORDER = ["FINDING", "VERIFICATION", "ROUTING", "DECISION", "RISK", "MCP"];
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

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="text-[10px] text-muted px-3 pt-2 pb-1.5 flex justify-between items-center shrink-0">
        <span className="font-semibold uppercase tracking-wider text-faint">CodeGraph & State</span>
      </div>

      {/* CodeGraph Section */}
      <div className="mx-2 mb-3 bg-panel border border-edge rounded-lg p-2.5 shrink-0">
        <div className="flex items-center justify-between pb-2 border-b border-edge/60">
          <div className="flex items-center gap-1.5 font-semibold text-txt text-[12px]">
            <span className="text-accent tracking-wide uppercase text-[10px]">CodeGraph</span>
          </div>
          <div className="flex items-center gap-1.5">
            {cg?.status === "indexing" ? (
              <span className="flex items-center gap-1 text-[9px] text-accent font-bold px-1.5 py-0.5 rounded bg-accent2 border border-accent/20">
                <Loader2 className="w-2.5 h-2.5 animate-spin" />
                INDEXING
              </span>
            ) : cg?.status === "ready" ? (
              <span className="text-[9px] text-good font-bold px-1.5 py-0.5 rounded bg-good/10 border border-good/20">
                READY
              </span>
            ) : cg?.status === "unsupported" ? (
              <span className="text-[9px] text-muted font-semibold px-1.5 py-0.5 rounded bg-panel2 border border-edge2">
                UNSUPPORTED
              </span>
            ) : (
              <span className="text-[9px] text-muted font-semibold px-1.5 py-0.5 rounded bg-panel2 border border-edge2">
                NONE
              </span>
            )}

            {cg && cg.status !== "none" && (
              <button
                onClick={handleReindex}
                disabled={reindexing || cg.status === "indexing"}
                className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-txt px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2"
              >
                {reindexing || cg.status === "indexing" ? "Indexing..." : "Re-index"}
              </button>
            )}
          </div>
        </div>

        {cg?.status === "none" ? (
          <div className="text-[11px] text-muted italic pt-2 px-0.5">No workspace open</div>
        ) : (
          <>
            <div className="grid grid-cols-3 gap-2 pt-2 text-[11px]">
              <div>
                <div className="text-faint text-[9px] uppercase tracking-wide">Nodes</div>
                <div className="font-semibold text-txt text-[11px] mt-0.5">
                  {cg?.nodes !== null && cg?.nodes !== undefined ? cg.nodes.toLocaleString() : "-"}
                </div>
              </div>
              <div>
                <div className="text-faint text-[9px] uppercase tracking-wide">Edges</div>
                <div className="font-semibold text-txt text-[11px] mt-0.5">
                  {cg?.edges !== null && cg?.edges !== undefined ? cg.edges.toLocaleString() : "-"}
                </div>
              </div>
              <div>
                <div className="text-faint text-[9px] uppercase tracking-wide">Files</div>
                <div className="font-semibold text-txt text-[11px] mt-0.5">
                  {cg?.files !== null && cg?.files !== undefined ? cg.files.toLocaleString() : "-"}
                </div>
              </div>
            </div>

            {cg && cg.languages && cg.languages.length > 0 && (
              <div className="mt-2 text-[10px] text-muted flex flex-wrap gap-1">
                {cg.languages.map((l) => (
                  <span key={l} className="bg-panel2 px-1 py-0.2 rounded border border-edge text-[9px] text-muted">
                    {l}
                  </span>
                ))}
              </div>
            )}

            {cg?.last_indexed && (
              <div className="mt-2 text-[8px] text-faint">
                Last indexed: {new Date(cg.last_indexed).toLocaleString()}
              </div>
            )}
          </>
        )}
      </div>

      <div className="text-[10px] text-muted px-3 pt-1 pb-1 flex justify-between items-center shrink-0">
        <span className="font-semibold uppercase tracking-wider text-faint">Artifacts ({artifacts.length})</span>
      </div>

      {/* Artifacts Pane */}
      <div className="flex-1 overflow-y-auto px-2 pb-2 flex flex-col gap-1.5">
        {artifacts.length === 0 && (
          <div className="text-[11px] text-muted italic px-2 py-1">Findings appear here as the pilot investigates.</div>
        )}

        {sortedGroupNames.map((groupName) => {
          const items = groupsMap.get(groupName) || [];
          const isCollapsed = !!collapsedGroups[groupName];
          const count = items.reduce((acc, it) => acc + it.count, 0);

          return (
            <div key={groupName} className="mb-1.5">
              {/* Group Header */}
              <button
                onClick={() => toggleGroup(groupName)}
                className="w-full flex items-center justify-between text-[10px] font-semibold text-muted hover:text-txt py-1 px-1.5 bg-panel/40 border border-edge/30 rounded mb-1 select-none transition-colors"
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
    </div>
  );
}
