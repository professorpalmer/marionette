import { useEffect, useRef, useState } from "react";
import { ChevronRight, Loader2, Send, Zap, Square } from "lucide-react";
import { api, type Config } from "../lib/api";
import PilotPicker from "./PilotPicker";

type Msg = { role: "user" | "assistant"; text: string };
type Card = {
  id: string; goal: string; cwd?: string | null;
  running: boolean; open: boolean;
  result?: { job_id: string; num: number; types: string[]; adapter: string;
             artifacts: { type: string; headline: string }[]; error?: string };
};
type Item = { kind: "msg"; msg: Msg } | { kind: "card"; card: Card };

export default function Conversation({ config, activeSessionId, onArtifacts, onJobChange }: {
  config: Config | null;
  activeSessionId: string | null;
  onArtifacts: (a: { type: string; headline: string }[]) => void;
  onJobChange: () => void;
}) {
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<"idle"|"thinking"|"executing"|"done"|"error">("idle");
  const [auto, setAuto] = useState(false);
  const [distillNotice, setDistillNotice] = useState<string | null>(null);
  const cancelRef = useRef<null | (() => void)>(null);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => { feedRef.current?.scrollTo(0, feedRef.current.scrollHeight); }, [items]);

  useEffect(() => {
    if (activeSessionId) {
      api.sessionTranscript(activeSessionId)
        .then((res) => {
          const loadedItems = (res.history || [])
            .filter((m: any) => m.role === "assistant" || (m.role === "user" && !m.content.startsWith("(")))
            .map((m: any) => ({
              kind: "msg" as const,
              msg: {
                role: m.role as "user" | "assistant",
                text: m.content || ""
              }
            }));
          setItems(loadedItems);
        })
        .catch(() => {
          setItems([]);
        });
    } else {
      setItems([]);
    }
  }, [activeSessionId]);

  const setCard = (id: string, patch: Partial<Card>) =>
    setItems((prev) => prev.map((it) =>
      it.kind === "card" && it.card.id === id ? { kind: "card", card: { ...it.card, ...patch } } : it));

  const send = () => {
    const msg = input.trim(); if (!msg || status === "thinking" || status === "executing") return;
    setItems((p) => [...p, { kind: "msg", msg: { role: "user", text: msg } }]);
    setInput(""); setStatus("thinking");
    const streamer = auto
      ? (cb: any, done: any, err: any) => api.auto(msg, cb, done, err)
      : (cb: any, done: any, err: any) => api.chat(msg, cb, done, err);
    cancelRef.current = streamer((ev: any) => {
      const d = ev.data || {};
      if (ev.kind === "message") {
        setStatus("thinking");
        setItems((p) => [...p, { kind: "msg", msg: { role: "assistant", text: d.text || "" } }]);
      } else if (ev.kind === "action_start") {
        setStatus("executing");
        setItems((p) => [...p, { kind: "card", card: {
          id: d.id, goal: d.goal, cwd: d.cwd, running: true, open: true } }]);
      } else if (ev.kind === "action_result") {
        setStatus("thinking");
        setCard(d.id, { running: false, open: false, result: d });
        if (d.artifacts && !d.error) onArtifacts(d.artifacts);
        onJobChange();
      } else if (ev.kind === "auto_status") {
        setStatus("executing");
      } else if (ev.kind === "distilled") {
        const parts: string[] = [];
        if (d.skill) {
          const { status, name, reason } = d.skill;
          if (status === "proposed") {
            parts.push(`proposed 1 skill${name ? ` ("${name}")` : ""}`);
          } else if (status === "duplicate") {
            parts.push("1 duplicate skill skipped");
          } else if (status === "skipped") {
            parts.push(`skill skipped${reason ? ` (${reason})` : ""}`);
          }
        }
        if (d.rules) {
          const { status, proposed, duplicates } = d.rules;
          const pCount = proposed?.length || 0;
          const dCount = duplicates?.length || 0;
          if (pCount > 0 && dCount > 0) {
            parts.push(`proposed ${pCount} rule${pCount === 1 ? "" : "s"} (${dCount} duplicate${dCount === 1 ? "" : "s"} skipped)`);
          } else if (pCount > 0) {
            parts.push(`proposed ${pCount} rule${pCount === 1 ? "" : "s"}`);
          } else if (dCount > 0) {
            parts.push(`${dCount} duplicate rule${dCount === 1 ? "" : "s"} skipped`);
          } else if (status === "duplicate") {
            parts.push("skipped duplicate rules");
          } else if (status === "skipped") {
            parts.push("skipped rules");
          }
        }
        if (parts.length > 0) {
          setDistillNotice(`Self-learning: ${parts.join(", ")} - review in Skills tab`);
        }
      } else if (ev.kind === "auto_halt") {
        setStatus("done");
        setItems((p) => [...p, { kind: "msg", msg: { role: "assistant", text: "HALT: " + (d.reason || "") } }]);
      } else if (ev.kind === "error") {
        setStatus("error");
        setItems((p) => [...p, { kind: "msg", msg: { role: "assistant", text: "[error] " + (d.error || "") } }]);
      }
    }, () => { setStatus("done"); cancelRef.current = null; },
       () => { setStatus("error"); cancelRef.current = null; });
  };

  const stop = () => { cancelRef.current?.(); cancelRef.current = null; setStatus("idle"); };

  return (
    <main className="flex flex-col h-full min-w-0 bg-bg">
      <header className="flex items-center justify-between px-6 border-b border-edge"
        style={{ paddingTop: 12, paddingBottom: 10, WebkitAppRegion: "drag" } as React.CSSProperties}>
        <span className="font-medium text-[13px] text-txt/90" style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}>Puppetmaster</span>
        <div style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}><StatusPill status={status} /></div>
      </header>

      <div ref={feedRef} className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6 flex flex-col gap-4">
          {items.length === 0 && (
            <div className="text-muted text-[13px] mt-32 text-center leading-relaxed">
              Message the pilot. It plans, investigates via swarms, and explains.
            </div>
          )}
          {items.map((it, i) => it.kind === "msg"
            ? <Bubble key={i} msg={it.msg} />
            : <ActionCard key={i} card={it.card} onToggle={() => setCard(it.card.id, { open: !it.card.open })} />)}
        </div>
      </div>

      <div className="px-6 pb-4 pt-1">
        <div className="max-w-3xl mx-auto">
          {distillNotice && (
            <div className="mb-3 px-3.5 py-2 bg-panel border border-warn/20 rounded-xl flex items-center justify-between text-[12px] shadow-lg text-txt/90">
              <span className="flex-1">
                {distillNotice}
              </span>
              <button
                onClick={() => setDistillNotice(null)}
                className="text-faint hover:text-muted transition font-medium text-[10.5px] ml-3 px-2 py-0.5 rounded border border-edge hover:bg-panel2"
              >
                Dismiss
              </button>
            </div>
          )}
          {/* compact composer: input + a single tidy control row */}
          <div className="bg-panel2/80 border border-edge rounded-2xl focus-within:border-edge2 shadow-lg shadow-black/20 transition">
            <textarea value={input} onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
              rows={1} placeholder={auto ? "Give the pilot an objective..." : "Message the pilot..."}
              className="w-full bg-transparent px-3.5 pt-2.5 pb-1 text-[13px] resize-none focus:outline-none max-h-32 placeholder:text-faint" />
            <div className="flex items-center gap-1.5 px-2 pb-1.5">
              <button onClick={() => setAuto((a) => !a)} title="Fully-Auto mode"
                className={`px-1.5 h-[22px] rounded-md text-[10.5px] flex items-center gap-1 transition
                  ${auto ? "bg-warn/15 text-warn" : "text-faint hover:text-muted"}`}>
                <Zap size={11} /> Auto
              </button>
              <div className="flex-1" />
              <PilotPicker config={config} />
              {status === "thinking" || status === "executing"
                ? <button onClick={stop} className="px-2 h-[22px] rounded-md bg-risk/15 text-risk text-[10.5px] font-medium flex items-center gap-1"><Square size={9} />Stop</button>
                : <button onClick={send} disabled={!input.trim()}
                    className="px-2.5 h-[22px] rounded-md bg-accent text-black/90 text-[10.5px] font-semibold flex items-center gap-1 hover:brightness-110 disabled:opacity-40 disabled:cursor-default transition">
                    <Send size={9} />{auto ? "Run" : "Send"}</button>}
            </div>
          </div>
        </div>
      </div>

    </main>
  );
}

function Bubble({ msg }: { msg: Msg }) {
  const isUser = msg.role === "user";
  return (
    <div className={`flex flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}>
      <span className="text-[10px] uppercase tracking-wider text-faint px-1">{isUser ? "you" : "pilot"}</span>
      <div className={`rounded-2xl px-3.5 py-2.5 text-[13px] leading-relaxed whitespace-pre-wrap break-words max-w-[85%]
        ${isUser ? "bg-accent2 text-txt" : "bg-panel border border-edge text-txt/90"}`}>{msg.text}</div>
    </div>
  );
}

function ActionCard({ card, onToggle }: { card: Card; onToggle: () => void }) {
  return (
    <div className="border border-edge rounded-lg bg-panel2 overflow-hidden">
      <button onClick={onToggle} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-panel text-left">
        {card.running ? <Loader2 size={12} className="animate-spin text-accent" /> : <span className="w-2 h-2 rounded-full bg-good" />}
        <span className="flex-1 text-[13px] truncate">Ran <b>swarm</b> &middot; {card.goal}</span>
        <ChevronRight size={13} className={`text-muted transition ${card.open ? "rotate-90" : ""}`} />
      </button>
      {card.open && (
        <div className="border-t border-edge px-3 py-2 bg-bg text-[12px]">
          <KV k="goal" v={card.goal} />
          {card.cwd && <KV k="cwd" v={card.cwd} />}
          {card.result?.error && <div className="text-risk mt-1">error: {card.result.error}</div>}
          {card.result && !card.result.error && (
            <>
              <KV k="job" v={card.result.job_id} />
              <KV k="found" v={`${card.result.num} artifacts · ${card.result.types.join(", ")}`} />
              {card.result.adapter === "demo" && <div className="text-warn text-[10px] mt-1">demo substrate -- not real codebase analysis</div>}
              {card.result.artifacts.map((a, i) => (
                <div key={i} className="flex gap-2 py-1 border-t border-edge/50 mt-1">
                  <span className="text-[9px] uppercase px-1.5 rounded bg-accent2 text-accent h-fit">{a.type}</span>
                  <span>{a.headline}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
const KV = ({ k, v }: { k: string; v: string }) => (
  <div className="flex gap-2 mb-0.5"><span className="text-muted w-11 shrink-0">{k}</span><span className="break-all">{v}</span></div>
);

function StatusPill({ status }: { status: string }) {
  const m: Record<string, string> = {
    idle: "text-faint", thinking: "text-accent", executing: "text-warn",
    done: "text-good", error: "text-risk",
  };
  const dot: Record<string, string> = {
    idle: "bg-faint", thinking: "bg-accent animate-pulse", executing: "bg-warn animate-pulse",
    done: "bg-good", error: "bg-risk",
  };
  return <span className={`text-[10.5px] flex items-center gap-1.5 ${m[status] || m.idle}`}>
    <span className={`w-1.5 h-1.5 rounded-full ${dot[status] || dot.idle}`} />{status}</span>;
}
