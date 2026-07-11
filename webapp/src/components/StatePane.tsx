import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, RefreshCw, ExternalLink } from "lucide-react";
import { api, type CodegraphStatus, type WikiStatusData } from "../lib/api";
import { lastSelectedProjectRoot, panelOpacityClass, useProjectSwitching } from "../lib/panelTransition";
import { useStaleWhileRevalidate } from "../lib/useStaleWhileRevalidate";

// Re-enable when a better presentation exists. Artifacts stay fully
// logged/stored on the backend; this flag is display-only.
const SHOW_ARTIFACTS = false;

export default function StatePane({ artifacts }: {
  artifacts: { type: string; headline: string; confidence?: number; id?: string; created_by?: string; [key: string]: any }[];
  embedded?: boolean;
}) {
  // CodeGraph status state
  const [reindexing, setReindexing] = useState(false);
  const [applyingExcludes, setApplyingExcludes] = useState(false);
  // Seed from the last announced root: a pane instance that mounts AFTER the
  // harness-project-selected event (tab switch, second window) would otherwise
  // stay rootless forever and render "CODEGRAPH none / WIKI off" while its
  // sibling shows live data.
  const [projectRoot, setProjectRoot] = useState(lastSelectedProjectRoot);
  const projectSwitching = useProjectSwitching();

  const {
    data: cg,
    isValidating: cgValidating,
    isTransitioning: cgTransitioning,
    isShowingStale: cgStale,
    revalidate: revalidateCg,
  } = useStaleWhileRevalidate<CodegraphStatus>(
    `codegraph:${projectRoot || "__none__"}`,
    () => api.getCodegraph(),
    { enabled: !!projectRoot },
  );

  const {
    data: wiki,
    isValidating: wikiValidating,
    isTransitioning: wikiTransitioning,
    isShowingStale: wikiStale,
    revalidate: revalidateWiki,
  } = useStaleWhileRevalidate<WikiStatusData>(
    `wiki-status:${projectRoot || "__none__"}`,
    () => api.getWikiStatus(),
    { enabled: !!projectRoot },
  );

  // Telemetry (CodeGraph / Wiki) is collapsed by default so it reads as a quiet
  // status line, not a wall of stats competing with the actual findings. The
  // full metrics are one click away. Preference persists per user.
  const [cgOpen, setCgOpen] = useState(() => localStorage.getItem("pmharness.statePane.cgOpen") === "1");
  const [wikiOpen, setWikiOpen] = useState(() => localStorage.getItem("pmharness.statePane.wikiOpen") === "1");
  const toggleCg = () => setCgOpen((v) => { localStorage.setItem("pmharness.statePane.cgOpen", v ? "0" : "1"); return !v; });
  const toggleWiki = () => setWikiOpen((v) => { localStorage.setItem("pmharness.statePane.wikiOpen", v ? "0" : "1"); return !v; });

  useEffect(() => {
    const onProject = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (typeof path === "string") setProjectRoot(path);
    };
    window.addEventListener("harness-project-selected", onProject);
    return () => window.removeEventListener("harness-project-selected", onProject);
  }, []);

  useEffect(() => {
    const onChange = () => {
      void revalidateCg();
      void revalidateWiki();
    };
    window.addEventListener("harness-config-changed", onChange);
    window.addEventListener("harness-new-session", onChange);
    return () => {
      window.removeEventListener("harness-config-changed", onChange);
      window.removeEventListener("harness-new-session", onChange);
    };
  }, [revalidateCg, revalidateWiki]);

  // Open the hosted wiki in the in-app Browser tab so a disconnected user has a
  // one-click path to create (or self-host) their portable LLM wiki. Mirrors the
  // markdown-link open flow: stash the URL, focus the tab, then dispatch the open.
  const openWikiSetup = async () => {
    // Prefer loopback handoff (?return=http://127.0.0.1:PORT/api/wiki/connect)
    // so Windows never routes marionette:// to the Microsoft Store.
    let url = "https://portablellm.wiki/welcome?client=marionette";
    try {
      const handoff = await api.startWikiHandoff();
      if (handoff?.setup_url) url = handoff.setup_url;
    } catch { /* fall back to client=marionette only */ }
    const ipc = (window as any).harnessIPC;
    if (ipc && typeof ipc.popoutBrowser === "function") {
      try {
        ipc.popoutBrowser(url);
        return;
      } catch { /* fall through to in-app browser tab */ }
    }
    (window as any).__pmPendingBrowserUrl = url;
    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "browser" }));
    window.dispatchEvent(new CustomEvent("harness-open-url", { detail: { url } }));
  };

  const disconnectWiki = async () => {
    try {
      await api.disconnectWiki();
      window.dispatchEvent(new Event("harness-config-changed"));
      await revalidateWiki();
    } catch (err) {
      console.error(err);
    }
  };

  useEffect(() => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || typeof ipc.onWikiConnected !== "function") return;
    return ipc.onWikiConnected(() => {
      void revalidateWiki();
      window.dispatchEvent(new Event("harness-config-changed"));
    });
  }, [revalidateWiki]);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    if (cg?.status === "indexing") {
      timer = setInterval(() => {
        void revalidateCg();
      }, 2000);
    }
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [cg?.status, revalidateCg]);

  const handleReindex = async () => {
    setReindexing(true);
    try {
      await api.reindexCodegraph();
      await revalidateCg();
    } catch (err) {
      console.error(err);
    } finally {
      setReindexing(false);
    }
  };

  const handleApplyExcludes = async () => {
    setApplyingExcludes(true);
    try {
      const excludes = cg?.suggested_action?.excludes
        || cg?.preflight?.suggested_excludes
        || [];
      await api.applyCodegraphExcludes(excludes);
      await revalidateCg();
    } catch (err) {
      console.error(err);
    } finally {
      setApplyingExcludes(false);
    }
  };

  const handleOpenSuggestedRoot = () => {
    const path = cg?.suggested_action?.path;
    if (!path) return;
    void api.openWorkspace(path).then(() => {
      window.dispatchEvent(new Event("harness-config-changed"));
      void revalidateCg();
    }).catch((err) => console.error(err));
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
  // telemetry pills. Defaults CLOSED: raw findings/decisions/risks are an
  // auditable "hidden advantage" people rarely browse, so the State tab leads
  // cleanly with CodeGraph + Wiki status and Artifacts is opt-in expand.
  // Preference persists per user (once toggled, their choice sticks).
  const [artifactsOpen, setArtifactsOpen] = useState(
    () => localStorage.getItem("pmharness.statePane.artifactsOpen") === "1",
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
  const cgNeedsScope = cg?.status === "needs_scope";
  const cgDot = cgReady ? "bg-good" : (cgIndexing || cgNeedsScope) ? "bg-accent" : "bg-faint";
  const cgWord = cgIndexing
    ? "indexing"
    : cgReady
      ? "ready"
      : cgNeedsScope
        ? "needs scope"
        : cg?.status === "unsupported"
          ? "unsupported"
          : "none";
  const cgMetric = cgReady && cg?.nodes != null
    ? `${cg.nodes.toLocaleString()} nodes`
    : cgIndexing
      ? "working"
      : cgNeedsScope
        ? "scope tree"
        : cg?.status === "none"
          ? "no workspace"
          : "";

  const wikiOk = wiki?.status === "ok";
  const wikiNeedsAuth = wiki?.status === "needs_auth";
  const wikiErr = wiki?.status === "error";
  const wikiDot = wikiOk
    ? "bg-good"
    : wikiNeedsAuth
      ? "bg-accent"
      : wikiErr
        ? "bg-risk"
        : "bg-faint";
  const wikiWord = wikiOk
    ? "connected"
    : wikiNeedsAuth
      ? "public only"
      : wikiErr
        ? "error"
        : "off";
  const wikiMetric = (wikiOk || wikiNeedsAuth)
    ? `${wiki?.page_count ?? 0} pages`
    : "";

  const statusDimmed = projectSwitching || cgTransitioning || wikiTransitioning;

  // SHOW_ARTIFACTS is currently false; keep processing above intact and touch
  // the derived bindings so noUnusedLocals does not fire on the gated UI.
  if (!SHOW_ARTIFACTS) {
    void sortedGroupNames;
    void artifactsOpen;
    void toggleArtifacts;
    void toggleGroup;
    void isGroupCollapsed;
    void groupsMap;
    void PLUMBING_GROUPS;
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Telemetry status strip: CodeGraph + Wiki collapsed to quiet one-line
          pills. This is proof-of-power chrome -- kept available, kept subdued,
          so the eye lands on the findings below instead of a wall of stats. */}
      <div className={`px-2 pt-2 pb-1.5 shrink-0 flex flex-col gap-1 ${panelOpacityClass(statusDimmed, cgStale || wikiStale)}`}>
        {/* CodeGraph pill */}
        <div className="rounded-md border border-edge/40 bg-panel/40 overflow-hidden">
          <button
            onClick={toggleCg}
            className="w-full flex items-center gap-2 px-2.5 py-1.5 text-[10px] hover:bg-panel2/30 transition-colors"
            title={cgOpen ? "Hide CodeGraph details" : "Show CodeGraph details"}
          >
            {cgOpen ? <ChevronDown className="w-3 h-3 text-faint shrink-0" /> : <ChevronRight className="w-3 h-3 text-faint shrink-0" />}
            <span className="uppercase tracking-wider font-semibold text-faint">CodeGraph</span>
            {cgValidating || cgIndexing
              ? <Loader2 className="w-2.5 h-2.5 animate-spin text-accent shrink-0" />
              : <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${cgDot}`} aria-hidden />}
            <span className="text-muted lowercase">{cgWord}</span>
            <span className="flex-1" />
            {cgMetric && <span className="text-faint tabular-nums truncate">{cgMetric}</span>}
          </button>

          {cgOpen && cg?.status !== "none" && (
            <div className="px-2.5 pb-2 pt-1 border-t border-edge/30">
              {cgNeedsScope && (
                <div className="mb-2 space-y-1.5">
                  <div className="text-[10px] text-warn leading-snug">
                    {cg.reason || "This workspace is too large or asset-heavy to index as a whole."}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {cg.suggested_action?.kind === "open_subdir" && cg.suggested_action.path && (
                      <button
                        type="button"
                        onClick={handleOpenSuggestedRoot}
                        className="text-[9px] bg-accent/15 hover:bg-accent/25 text-accent px-1.5 py-0.5 rounded transition-colors font-medium border border-accent/30"
                      >
                        Open source subdir
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={handleApplyExcludes}
                      disabled={applyingExcludes || reindexing}
                      className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2"
                    >
                      {applyingExcludes ? "Applying..." : "Apply asset excludes"}
                    </button>
                    <button
                      type="button"
                      onClick={handleReindex}
                      disabled={reindexing || applyingExcludes}
                      className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2"
                    >
                      {reindexing ? "Indexing..." : "Retry index"}
                    </button>
                  </div>
                </div>
              )}

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
                {!cgNeedsScope && (
                  <button
                    onClick={handleReindex}
                    disabled={reindexing || cgIndexing}
                    className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2 shrink-0"
                  >
                    {reindexing || cgIndexing ? "Indexing..." : "Re-index"}
                  </button>
                )}
              </div>

              {(cgIndexing || cg?.status === "unsupported") && cg?.reason && (
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
            {wikiValidating
              ? <Loader2 className="w-2.5 h-2.5 animate-spin text-faint shrink-0" />
              : <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${wikiDot}`} aria-hidden />}
            <span className="text-muted lowercase">{wikiWord}</span>
            <span className="flex-1" />
            {wikiMetric && <span className="text-faint tabular-nums truncate">{wikiMetric}</span>}
          </button>

          {wikiOpen && (
            <div className="px-2.5 pb-2 pt-1.5 border-t border-edge/30 text-[10px]">
              {wikiErr ? (
                <div className="flex flex-col gap-1.5">
                  <div className="text-risk italic">{wiki?.error || "Failed to fetch wiki status"}</div>
                  <div className="flex items-center justify-between gap-2">
                    {wiki?.base_url
                      ? <span className="text-[8px] text-faint truncate">{wiki.base_url}</span>
                      : <span />}
                    <button
                      onClick={() => void revalidateWiki()}
                      disabled={wikiValidating}
                      className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2 flex items-center justify-center shrink-0"
                      title="Re-check wiki connection"
                    >
                      <RefreshCw className={`w-2.5 h-2.5 ${wikiValidating ? "animate-spin" : ""}`} />
                    </button>
                  </div>
                </div>
              ) : wikiNeedsAuth ? (
                <div className="flex flex-col gap-1.5">
                  <div className="text-warn leading-relaxed">
                    {wiki?.hint
                      || "Connected at public tier only. Paste your personal LLM URL or owner token in Settings → Wiki Graph."}
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[11px]">
                    <div>
                      <div className="text-faint text-[9px] uppercase tracking-wide">Pages</div>
                      <div className="font-semibold text-muted text-[11px] mt-0.5 tabular-nums">
                        {(wiki?.page_count ?? 0).toLocaleString()}
                      </div>
                    </div>
                    <div>
                      <div className="text-faint text-[9px] uppercase tracking-wide">Links</div>
                      <div className="font-semibold text-muted text-[11px] mt-0.5 tabular-nums">
                        {(wiki?.link_count ?? 0).toLocaleString()}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <button
                      onClick={() => void openWikiSetup()}
                      className="text-[9px] bg-accent/15 hover:bg-accent/25 text-accent px-2 py-0.5 rounded transition-colors font-medium border border-accent/30 flex items-center gap-1 shrink-0"
                      title="Open portablellm.wiki Owner console"
                    >
                      <ExternalLink className="w-2.5 h-2.5" /> Owner console
                    </button>
                    <button
                      onClick={() => void disconnectWiki()}
                      className="text-[9px] bg-edge hover:bg-risk/20 text-muted hover:text-risk px-2 py-0.5 rounded transition-colors font-medium border border-edge2 shrink-0"
                      title="Clear wiki URL and token so you can reconnect"
                    >
                      Disconnect
                    </button>
                    <button
                      onClick={() => void revalidateWiki()}
                      disabled={wikiValidating}
                      className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2 flex items-center justify-center shrink-0"
                      title="Re-check wiki connection"
                    >
                      <RefreshCw className={`w-2.5 h-2.5 ${wikiValidating ? "animate-spin" : ""}`} />
                    </button>
                  </div>
                  {wiki?.base_url
                    ? <span className="text-[8px] text-faint truncate">{wiki.base_url}</span>
                    : null}
                </div>
              ) : !wikiOk ? (
                <div className="flex flex-col gap-1.5">
                  <div className="text-faint leading-relaxed">
                    A portable LLM wiki is a personal, URL-addressable briefing about
                    you that any model can read. Click below to open portablellm.wiki
                    in a pop-out browser — sign up (or sign in), and Marionette links
                    automatically at owner tier. No token paste.
                  </div>
                  <div className="flex items-center gap-1.5">
                    <button
                      onClick={() => void openWikiSetup()}
                      className="text-[9px] bg-accent/15 hover:bg-accent/25 text-accent px-2 py-0.5 rounded transition-colors font-medium border border-accent/30 flex items-center gap-1 shrink-0"
                      title="Open portablellm.wiki signup in a pop-out browser"
                    >
                      <ExternalLink className="w-2.5 h-2.5" /> Connect portablellm.wiki
                    </button>
                    <button
                      onClick={() => void revalidateWiki()}
                      disabled={wikiValidating}
                      className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2 flex items-center justify-center shrink-0"
                      title="Re-check wiki connection"
                    >
                      <RefreshCw className={`w-2.5 h-2.5 ${wikiValidating ? "animate-spin" : ""}`} />
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  <div className="grid grid-cols-2 gap-2 text-[11px]">
                    <div>
                      <div className="text-faint text-[9px] uppercase tracking-wide">Pages</div>
                      <div className="font-semibold text-muted text-[11px] mt-0.5 tabular-nums">
                        {(wiki?.page_count ?? 0).toLocaleString()}
                      </div>
                    </div>
                    <div>
                      <div className="text-faint text-[9px] uppercase tracking-wide">Links</div>
                      <div className="font-semibold text-muted text-[11px] mt-0.5 tabular-nums">
                        {(wiki?.link_count ?? 0).toLocaleString()}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    {wiki?.base_url
                      ? <span className="text-[8px] text-faint truncate">{wiki.base_url}</span>
                      : <span />}
                    <div className="flex items-center gap-1.5 shrink-0">
                      <button
                        onClick={() => void disconnectWiki()}
                        className="text-[9px] bg-edge hover:bg-risk/20 text-muted hover:text-risk px-2 py-0.5 rounded transition-colors font-medium border border-edge2"
                        title="Clear wiki URL and token"
                      >
                        Disconnect
                      </button>
                      <button
                        onClick={() => void revalidateWiki()}
                        disabled={wikiValidating}
                        className="text-[9px] bg-edge hover:bg-edge2 disabled:opacity-50 text-muted px-1.5 py-0.5 rounded transition-colors font-medium border border-edge2 flex items-center justify-center"
                        title="Refresh Wiki Stats"
                      >
                        <RefreshCw className={`w-2.5 h-2.5 ${wikiValidating ? "animate-spin" : ""}`} />
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Artifacts: gated behind SHOW_ARTIFACTS. Processing above stays live so
          re-enabling the flag restores the UI without rewiring data flow. */}
      {SHOW_ARTIFACTS && (
        <>
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
        </>
      )}
    </div>
  );
}
