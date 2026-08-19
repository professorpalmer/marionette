import { useEffect, useState } from "react";
import { Brain, Trash2, Plus } from "lucide-react";
import { api } from "../lib/api";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";

interface MemoryEntry {
  id: string;
  text: string;
  category: string;
  created_at: number;
  source: string;
}

const CATEGORIES = ["general", "preference", "environment", "fact", "convention"];

const getCategoryColor = (cat: string) => {
  switch (cat) {
    case "preference":
      return "bg-accent/10 text-accent border-accent/20";
    case "environment":
      return "bg-good/15 text-good border-good/25";
    case "fact":
      return "bg-warn/15 text-warn border-warn/25";
    case "convention":
      return "bg-indigo-500/15 text-indigo-400 border-indigo-500/25";
    default:
      return "bg-panel2 text-muted border-edge/50";
  }
};

export default function MemoryPane({ embedded = false }: { embedded?: boolean }) {
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [totalChars, setTotalChars] = useState(0);
  const [limit, setLimit] = useState(4000);
  const [newText, setNewText] = useState("");
  const [newCategory, setNewCategory] = useState("general");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const msgNotice = usePanelNotice(msg || null);

  const refresh = async () => {
    try {
      const res = await api.memory();
      setEntries(res.memory || []);
      setTotalChars(res.total_chars ?? 0);
      setLimit(res.limit ?? 4000);
    } catch (err: any) {
      setMsg(err?.error || "Failed to load memory");
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  const handleAdd = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!newText.trim() || busy === "add") return;
    setBusy("add");
    setMsg("");
    try {
      await api.memoryAdd(newText.trim(), newCategory);
      setNewText("");
      await refresh();
    } catch (err: any) {
      setMsg(err?.error || "Failed to add memory");
    } finally {
      setBusy("");
    }
  };

  const handleDelete = async (id: string) => {
    if (busy) return;
    setBusy(id);
    setMsg("");
    try {
      await api.memoryRemove(id);
      await refresh();
    } catch (err: any) {
      setMsg(err?.error || "Failed to delete memory");
    } finally {
      setBusy("");
    }
  };

  const percent = limit > 0 ? Math.min(100, (totalChars / limit) * 100) : 0;
  const isOverLimit = percent >= 90;

  return (
    <div className={embedded ? "text-[12px] flex flex-col gap-2" : "flex flex-col h-full text-[12px]"}>
      {!embedded && (
        <div className="flex items-center justify-between px-3 py-2 border-b border-edge">
          <span className="uppercase tracking-wider text-[10px] text-faint font-medium flex items-center gap-1.5">
            <Brain size={11} /> Durable memory
          </span>
        </div>
      )}

      <div className={embedded ? "space-y-2" : "flex-1 overflow-y-auto p-2 flex flex-col gap-2"}>
        <div className="flex items-center justify-between px-1 mb-0.5">
          <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1.5">
            <Brain size={11} /> Durable memory
          </span>
          <span className="text-[10px] text-muted">
            {totalChars} / {limit} chars
          </span>
        </div>

        <div className="w-full bg-panel/30 h-1 rounded-full overflow-hidden mb-3 border border-edge/10">
          <div
            className={`h-full transition-all duration-300 ${isOverLimit ? "bg-warn" : "bg-accent"}`}
            style={{ width: `${percent}%` }}
          />
        </div>

        {msgNotice && <div className="text-[10px] text-muted px-1 mb-2">{msgNotice}</div>}

        <form onSubmit={handleAdd} className="space-y-1.5 mb-3 bg-panel2/20 p-2 rounded border border-edge/30">
          <div className="text-[10px] uppercase tracking-wider text-faint font-semibold">
            Add Memory Entry
          </div>
          <div className="flex gap-1.5">
            <input
              type="text"
              placeholder="Add a durable fact or preference..."
              value={newText}
              onChange={(e) => setNewText(e.target.value)}
              className="flex-1 bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent"
              disabled={busy === "add"}
            />
            <select
              value={newCategory}
              onChange={(e) => setNewCategory(e.target.value)}
              className="bg-panel2 border border-edge rounded px-1.5 py-1 text-txt text-[11px] focus:outline-none focus:border-accent"
              disabled={busy === "add"}
            >
              {CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>
                  {cat}
                </option>
              ))}
            </select>
            <button
              type="submit"
              disabled={!newText.trim() || busy === "add"}
              className="px-2.5 py-1 rounded bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 text-[11px] font-semibold transition-colors disabled:opacity-40 flex items-center gap-1"
            >
              <Plus size={11} /> Add
            </button>
          </div>
        </form>

        <div className="space-y-1.5">
          {entries.length === 0 ? (
            <div className="text-faint text-[10px] px-1 py-2 italic">
              No durable memories yet. The agent will save preferences and facts here as it learns them, or add one above.
            </div>
          ) : (
            entries.map((item) => (
              <div
                key={item.id}
                className="border border-edge rounded-lg p-2 bg-panel2/40 flex flex-col gap-1.5"
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="text-txt text-[11px] whitespace-pre-wrap break-words flex-1 leading-relaxed">
                    {item.text}
                  </span>
                  <button
                    onClick={() => handleDelete(item.id)}
                    disabled={busy !== ""}
                    className="text-muted hover:text-risk disabled:opacity-40 p-0.5"
                    title="Delete memory"
                  >
                    <Trash2 size={11} />
                  </button>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className={`text-[9px] uppercase tracking-wider px-1 rounded border font-medium ${getCategoryColor(item.category)}`}>
                    {item.category}
                  </span>
                  <span className="text-[9px] text-faint uppercase tracking-wider">
                    via {item.source}
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
