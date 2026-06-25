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

export default function Conversation({ config, onArtifacts, onJobChange }: {
  config: Config | null;
  onArtifacts: (a: { type: string; headline: string }[]) => void;
  onJobChange: () => void;
}) {
  const [items, setItems] = useState<Item[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<"idle"|"thinking"|"executing"|"done"|"error">("idle");
  const [auto, setAuto] = useState(false);
  const cancelRef = useRef<null | (() => void)>(null);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => { feedRef.current?.scrollTo(0, feedRef.current.scrollHeight); }, [items]);

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
      <header className="flex items-center justify-between px-4 py-3 border-b border-edge">
        <span className="font-semibold text-sm">PM-Native Harness</span>
        <div className="flex items-center gap-2">
          <PilotPicker config={config} />
          <StatusPill status={status} />
        </div>
      </header>

      <div ref={feedRef} className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-3">
        {items.length === 0 && (
          <div className="text-muted text-sm m-auto text-center">
            Message the pilot. It plans, investigates via swarms, and explains.
          </div>
        )}
        {items.map((it, i) => it.kind === "msg"
          ? <Bubble key={i} msg={it.msg} />
          : <ActionCard key={i} card={it.card} onToggle={() => setCard(it.card.id, { open: !it.card.open })} />)}
      </div>

      <div className="border-t border-edge p-3">
        <div className="flex items-end gap-2">
          <button onClick={() => setAuto((a) => !a)} title="Fully-Auto mode"
            className={`px-2.5 h-9 rounded-lg border text-xs flex items-center gap-1 transition
              ${auto ? "bg-warn/20 border-warn/40 text-warn" : "bg-panel2 border-edge text-muted"}`}>
            <Zap size={13} /> Auto
          </button>
          <textarea value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
            rows={1} placeholder={auto ? "Give the pilot an objective to pursue autonomously..." : "Message the pilot..."}
            className="flex-1 bg-panel2 border border-edge rounded-lg px-3 py-2 text-sm resize-none
                       focus:outline-none focus:border-accent2 max-h-32" />
          {status === "thinking" || status === "executing"
            ? <button onClick={stop} className="px-4 h-9 rounded-lg bg-risk/20 border border-risk/40 text-risk text-sm font-semibold flex items-center gap-1"><Square size={13} />Stop</button>
            : <button onClick={send} className="px-4 h-9 rounded-lg bg-accent2 border border-accent2 text-accent text-sm font-semibold flex items-center gap-1 hover:brightness-125"><Send size={13} />{auto ? "Run" : "Send"}</button>}
        </div>
      </div>
    </main>
  );
}

function Bubble({ msg }: { msg: Msg }) {
  const isUser = msg.role === "user";
  return (
    <div className={`flex gap-2 max-w-[88%] ${isUser ? "self-end flex-row-reverse" : ""}`}>
      <div className="text-[10px] uppercase tracking-wide text-muted pt-1.5 w-9 shrink-0">{isUser ? "you" : "pilot"}</div>
      <div className={`rounded-xl px-3 py-2 text-sm whitespace-pre-wrap break-words border
        ${isUser ? "bg-accent2/60 border-accent2" : "bg-panel border-edge"}`}>{msg.text}</div>
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
    idle: "text-muted border-edge", thinking: "text-accent border-accent2",
    executing: "text-warn border-warn/40", done: "text-good border-good/40",
    error: "text-risk border-risk/40",
  };
  return <span className={`text-[11px] px-2.5 py-0.5 rounded-full border ${m[status] || m.idle}`}>{status}</span>;
}
