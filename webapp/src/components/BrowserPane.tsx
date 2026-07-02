import { useEffect, useRef, useState } from "react";
import { RotateCw, ExternalLink, ArrowLeft, ArrowRight, Plus, X } from "lucide-react";

// In-app browser pane with multi-tab support.
// Each tab maintains its own URL, loading state, history, and active iframe/webview.
const DEFAULT_URL = "https://duckduckgo.com";

interface Tab {
  id: string;
  url: string;
  // The URL the webview's src is bound to. Set ONCE at tab creation (and only
  // changed by an explicit address-bar navigation), never by post-load redirect
  // tracking. Binding the webview src to the live `url` caused React to re-drive
  // the webview on every redirect -- interrupting login flows and bouncing back
  // to the login screen in a refresh loop.
  initialUrl: string;
  title: string;
  loading: boolean;
  canBack: boolean;
  canFwd: boolean;
  nonce: number;
}

export default function BrowserPane() {
  const isDesktop = !!(window as any).harnessIPC;

  const initialIdRef = useRef(Math.random().toString(36).substring(2, 11));
  const [tabs, setTabs] = useState<Tab[]>([
    {
      id: initialIdRef.current,
      url: DEFAULT_URL,
      initialUrl: DEFAULT_URL,
      title: "New Tab",
      loading: false,
      canBack: false,
      canFwd: false,
      nonce: 0,
    }
  ]);
  const [activeTabId, setActiveTabId] = useState<string>(initialIdRef.current);

  const [draft, setDraft] = useState(DEFAULT_URL);
  const [editing, setEditing] = useState(false);

  const webviewsRef = useRef<Record<string, any>>({});

  // Open a URL dispatched from elsewhere (e.g. a clicked chat link) in a fresh
  // tab, so following a link never clobbers the tab the user was already on.
  useEffect(() => {
    const openInNewTab = (url: string) => {
      const id = Math.random().toString(36).substring(2, 11);
      setTabs((prev) => [
        ...prev,
        { id, url, initialUrl: url, title: "New Tab", loading: true, canBack: false, canFwd: false, nonce: 0 },
      ]);
      setActiveTabId(id);
    };
    const onOpenUrl = (e: Event) => {
      const url = (e as CustomEvent<{ url?: string }>).detail?.url;
      if (!url) return;
      (window as any).__pmPendingBrowserUrl = null;
      openInNewTab(url);
    };
    window.addEventListener("harness-open-url", onOpenUrl as EventListener);
    // A link clicked while this pane was unmounted stashes its URL; the focus
    // that mounts us fires the event before this listener exists, so consume the
    // stash here to cover that race.
    const pending = (window as any).__pmPendingBrowserUrl;
    if (pending) {
      (window as any).__pmPendingBrowserUrl = null;
      openInNewTab(pending);
    }
    return () => window.removeEventListener("harness-open-url", onOpenUrl as EventListener);
  }, []);

  const activeTab = tabs.find((t) => t.id === activeTabId) || tabs[0];
  const url = activeTab?.url || DEFAULT_URL;
  const loading = activeTab?.loading || false;
  const canBack = activeTab?.canBack || false;
  const canFwd = activeTab?.canFwd || false;

  useEffect(() => {
    if (!editing && activeTab) {
      setDraft(activeTab.url);
    }
  }, [editing, activeTabId, activeTab?.url]);

  const normalize = (raw: string): string => {
    const v = raw.trim();
    if (!v) return url;
    if (/^https?:\/\//i.test(v)) return v;
    if (/^[\w-]+(\.[\w-]+)+/.test(v)) return "https://" + v;  // looks like a domain
    return "https://duckduckgo.com/?q=" + encodeURIComponent(v); // else search
  };

  const go = (raw: string) => {
    const next = normalize(raw);
    setTabs((prev) =>
      prev.map((t) => (t.id === activeTabId ? { ...t, url: next, initialUrl: next, loading: true } : t))
    );
    if (isDesktop) {
      const wv = webviewsRef.current[activeTabId];
      if (wv) {
        try {
          wv.loadURL(next);
        } catch {}
      }
    } else {
      setTabs((prev) =>
        prev.map((t) => (t.id === activeTabId ? { ...t, nonce: t.nonce + 1 } : t))
      );
    }
  };

  const reload = () => {
    setTabs((prev) =>
      prev.map((t) => (t.id === activeTabId ? { ...t, loading: true } : t))
    );
    if (isDesktop) {
      const wv = webviewsRef.current[activeTabId];
      if (wv) {
        try {
          wv.reload();
        } catch {}
      }
    } else {
      setTabs((prev) =>
        prev.map((t) => (t.id === activeTabId ? { ...t, nonce: t.nonce + 1 } : t))
      );
    }
  };

  const back = () => {
    if (isDesktop) {
      const wv = webviewsRef.current[activeTabId];
      if (wv?.canGoBack?.()) {
        wv.goBack();
      }
    }
  };

  const fwd = () => {
    if (isDesktop) {
      const wv = webviewsRef.current[activeTabId];
      if (wv?.canGoForward?.()) {
        wv.goForward();
      }
    }
  };

  const newTab = (initialUrl = DEFAULT_URL) => {
    const id = Math.random().toString(36).substring(2, 11);
    const tab: Tab = {
      id,
      url: initialUrl,
      initialUrl,
      title: "New Tab",
      loading: false,
      canBack: false,
      canFwd: false,
      nonce: 0,
    };
    setTabs((prev) => [...prev, tab]);
    setActiveTabId(id);
  };

  const closeTab = (id: string, e?: React.MouseEvent) => {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    setTabs((prev) => {
      const remaining = prev.filter((t) => t.id !== id);
      if (remaining.length === 0) {
        const newId = Math.random().toString(36).substring(2, 11);
        setActiveTabId(newId);
        return [{
          id: newId,
          url: DEFAULT_URL,
          initialUrl: DEFAULT_URL,
          title: "New Tab",
          loading: false,
          canBack: false,
          canFwd: false,
          nonce: 0,
        }];
      }
      if (activeTabId === id) {
        const idx = prev.findIndex((t) => t.id === id);
        const nextActive = prev[idx + 1] || prev[idx - 1];
        if (nextActive) {
          setActiveTabId(nextActive.id);
        }
      }
      return remaining;
    });
  };

  return (
    <div className="flex flex-col h-full bg-panel">
      {/* Tab strip */}
      <div className="flex items-center gap-1 px-2 pt-1.5 bg-panel border-b border-edge select-none overflow-x-auto scrollbar-none">
        {tabs.map((tab) => {
          const isActive = tab.id === activeTabId;
          
          let displayTitle = tab.title;
          if (!displayTitle || displayTitle === "New Tab" || displayTitle === DEFAULT_URL) {
            try {
              displayTitle = new URL(tab.url).hostname;
            } catch {
              displayTitle = "New Tab";
            }
          }
          
          return (
            <div
              key={tab.id}
              onClick={() => setActiveTabId(tab.id)}
              className={`group relative flex items-center gap-2 px-3 py-1 rounded-t-md text-[11px] font-medium cursor-pointer transition max-w-[140px] min-w-[80px] shrink-0 border-t-2
                ${isActive 
                  ? "bg-panel2 text-txt border-x border-b border-x-edge border-b-panel2 border-t-accent -mb-[1px]" 
                  : "bg-panel text-muted border-transparent hover:bg-panel2/50 hover:text-txt"
                }`}
            >
              <span className="truncate flex-1 pr-3">{displayTitle}</span>
              <button
                onClick={(e) => closeTab(tab.id, e)}
                className={`absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 rounded-md hover:bg-panel2 hover:text-txt transition
                  ${isActive ? "text-muted" : "opacity-0 group-hover:opacity-100 text-faint"}`}
              >
                <X size={10} />
              </button>
            </div>
          );
        })}
        
        <button
          onClick={() => newTab()}
          title="New Tab"
          className="p-1.5 rounded-md text-muted hover:text-txt hover:bg-panel2 transition shrink-0"
        >
          <Plus size={12} />
        </button>
      </div>

      <div className="flex items-center gap-1 px-2 py-1.5 border-b border-edge bg-panel">
        {isDesktop && <NavBtn label="Back" onClick={back} disabled={!canBack}><ArrowLeft size={12} /></NavBtn>}
        {isDesktop && <NavBtn label="Forward" onClick={fwd} disabled={!canFwd}><ArrowRight size={12} /></NavBtn>}
        <NavBtn label="Reload" onClick={reload}><RotateCw size={12} className={loading ? "animate-spin" : ""} /></NavBtn>
        <form onSubmit={(e) => { e.preventDefault(); setEditing(false); go(draft); }} className="flex-1">
          <input value={draft} onChange={(e) => setDraft(e.target.value)}
            onFocus={() => setEditing(true)} onBlur={() => setEditing(false)}
            spellCheck={false}
            className="w-full bg-bg border border-edge rounded-md px-2 h-6 text-[11px] text-txt
                       focus:outline-none focus:border-accent2" />
        </form>
        <NavBtn label="Open externally" onClick={() => window.open(url, "_blank")}><ExternalLink size={12} /></NavBtn>
      </div>

      <div className="flex-1 relative overflow-hidden bg-bg" style={{ backgroundColor: "#0f1113" }}>
        {tabs.map((tab) => {
          const isActive = tab.id === activeTabId;
          return isDesktop ? (
            <webview
              key={`webview-${tab.id}`}
              ref={(el: any) => {
                if (el) {
                  if (webviewsRef.current[tab.id] === el) return;
                  // Clean up old listeners
                  const oldEl = webviewsRef.current[tab.id];
                  if (oldEl) {
                    try {
                      oldEl.removeEventListener("did-start-loading", oldEl._onStart);
                      oldEl.removeEventListener("did-stop-loading", oldEl._onStop);
                    } catch {}
                  }

                  webviewsRef.current[tab.id] = el;

                  const onStart = () => {
                    setTabs((prev) =>
                      prev.map((t) => (t.id === tab.id ? { ...t, loading: true } : t))
                    );
                  };

                  const onStop = () => {
                    try {
                      const u = el.getURL();
                      let title = "";
                      try { title = el.getTitle(); } catch {}
                      if (!title && u) {
                        try { title = new URL(u).hostname; } catch { title = u; }
                      }
                      const cb = el.canGoBack();
                      const cf = el.canGoForward();
                      setTabs((prev) =>
                        prev.map((t) =>
                          t.id === tab.id
                            ? {
                                ...t,
                                loading: false,
                                url: u || t.url,
                                title: title || t.title,
                                canBack: cb,
                                canFwd: cf,
                              }
                            : t
                        )
                      );
                    } catch {
                      setTabs((prev) =>
                        prev.map((t) => (t.id === tab.id ? { ...t, loading: false } : t))
                      );
                    }
                  };

                  el._onStart = onStart;
                  el._onStop = onStop;

                  el.addEventListener("did-start-loading", onStart);
                  el.addEventListener("did-stop-loading", onStop);
                } else {
                  const oldEl = webviewsRef.current[tab.id];
                  if (oldEl) {
                    try {
                      oldEl.removeEventListener("did-start-loading", oldEl._onStart);
                      oldEl.removeEventListener("did-stop-loading", oldEl._onStop);
                    } catch {}
                  }
                  delete webviewsRef.current[tab.id];
                }
              }}
              src={tab.initialUrl}
              // @ts-expect-error -- webview is an Electron element, not in React JSX types
              allowpopups="true"
              // Persistent session partition: cookies + localStorage survive
              // webview remounts and navigations. Without this the webview gets a
              // fresh in-memory session each render, wiping the auth cookie right
              // after login -- which bounced the user back to the login screen in a
              // refresh loop (Twitter/X etc.). A shared "persist:browser" partition
              // also keeps you logged in across tabs and app restarts.
              partition="persist:browser"
              className="absolute inset-0 w-full h-full border-0"
              style={{
                display: isActive ? "flex" : "none",
                width: "100%",
                height: "100%",
                backgroundColor: "#0f1113",
              }}
            />
          ) : (
            <div
              key={`iframe-container-${tab.id}`}
              className="absolute inset-0 w-full h-full"
              style={{ display: isActive ? "block" : "none" }}
            >
              <iframe
                key={`iframe-${tab.id}-${tab.nonce}`}
                src={tab.url}
                title={`browser-${tab.id}`}
                onLoad={() => {
                  setTabs((prev) =>
                    prev.map((t) => (t.id === tab.id ? { ...t, loading: false } : t))
                  );
                }}
                className="w-full h-full border-0"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
              />
            </div>
          );
        })}

        {!isDesktop && (
          <div className="absolute bottom-0 inset-x-0 bg-panel/95 border-t border-edge px-3 py-1.5 text-[10px] text-muted z-10">
            Web preview: many sites block embedding (X-Frame-Options/CSP). Full
            navigation arrives in the desktop build via the webview.
          </div>
        )}
      </div>
    </div>
  );
}

function NavBtn({ label, onClick, children, disabled }: any) {
  return (
    <button title={label} onClick={onClick} type="button" disabled={disabled}
      className="grid place-items-center size-6 shrink-0 rounded-md text-muted hover:text-txt hover:bg-panel2 transition disabled:opacity-40 disabled:hover:text-muted disabled:hover:bg-transparent">
      {children}
    </button>
  );
}
