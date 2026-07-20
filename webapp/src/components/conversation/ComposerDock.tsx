/**
 * Bottom composer dock: wiki/memory notices, queues, autocomplete, textarea.
 * State and send/stop wiring stay owned by Conversation.tsx.
 */

import type { RefObject } from "react";
import {
  ChevronDown,
  ChevronUp,
  Code,
  FileText,
  GripVertical,
  Image as ImageIcon,
  ListChecks,
  Loader2,
  Pencil,
  Send,
  Share2,
  Square,
  Trash2,
  X,
  Zap,
  Brain,
} from "lucide-react";
import { api, type Config, type ContextUsageResponse } from "../../lib/api";
import PilotPicker from "../PilotPicker";
import WorkspaceChip from "./WorkspaceChip";
import { formatMentionListingCapMessage, type MentionListingCap } from "./slashCommands";
import { filterSlashCommands } from "./composerInput";
import {
  CONTEXT_USAGE_COLORS,
  contextUsagePercent,
  formatTokenK,
} from "./contextUsageColors";

export type AttachedImage = { path: string; name: string; previewUrl: string };
export type MsgQueueItem = { text: string; auto: boolean; plan?: boolean };
export type ServerQueueItem = { id: string; text: string; images?: string[]; model?: string };
export type MemoryProposal = { id: string; text: string; category: string };
export type SymbolHit = { name: string; kind: string; path: string; line: number };
export type SlashCommand = { cmd: string; desc: string; scope?: string };

export default function ComposerDock({
  config,
  taRef,
  input,
  auto,
  plan,
  composerBusy,
  transcriptStale,
  wikiPrepared,
  memoryProposals,
  distillNotice,
  msgQueue,
  dragIndex,
  dragOverIndex,
  queueItems,
  queueDragIndex,
  queueDragOverIndex,
  editingIndex,
  canRevertEdit,
  editNotice,
  editBusy,
  showContextPanel,
  contextUsage,
  mentionSearch,
  filteredFiles,
  symbolResults,
  mentionListingCap,
  selectedFileIndex,
  codegraphStatus,
  slashSearch,
  selectedSlashIndex,
  allSlashCommands,
  attachedImages,
  isDragOver,
  uploadError,
  onSetWikiPrepared,
  onSetMemoryProposals,
  onSetDistillNotice,
  onSetMsgQueue,
  onSetInput,
  onSetAuto,
  onSetPlan,
  onSetCanRevertEdit,
  onSetEditNotice,
  onSetShowContextPanel,
  onSetSelectedFileIndex,
  onSetSelectedSlashIndex,
  onSetAttachedImages,
  onSetUploadError,
  onSetLightboxUrl,
  setSafeTimeout,
  fetchContextUsage,
  handleDragStart,
  handleDragOver,
  handleDragLeave,
  handleDrop,
  handleDragEnd,
  moveQueueItem,
  handleQueueClearAll,
  handleQueueDragStart,
  handleQueueDragOver,
  handleQueueDragLeave,
  handleQueueDrop,
  handleQueueDragEnd,
  handleQueueEdit,
  handleQueueRemove,
  handleComposerDragOver,
  handleComposerDragLeave,
  handleComposerDrop,
  handleRevertEdit,
  handleCancelEdit,
  handleInputChange,
  handleKeyDown,
  handlePaste,
  insertMention,
  insertSymbol,
  insertSlashCommand,
  handleQueueAdd,
  stop,
  send,
}: {
  config: Config | null;
  taRef: RefObject<HTMLTextAreaElement | null>;
  input: string;
  auto: boolean;
  plan: boolean;
  composerBusy: boolean;
  transcriptStale: boolean;
  wikiPrepared: { pages: any[]; autoIngested: boolean } | null;
  memoryProposals: MemoryProposal[];
  distillNotice: string | null;
  msgQueue: MsgQueueItem[];
  dragIndex: number | null;
  dragOverIndex: number | null;
  queueItems: ServerQueueItem[];
  queueDragIndex: number | null;
  queueDragOverIndex: number | null;
  editingIndex: number | null;
  canRevertEdit: boolean;
  editNotice: string | null;
  editBusy: boolean;
  showContextPanel: boolean;
  contextUsage: ContextUsageResponse | null;
  mentionSearch: string | null;
  filteredFiles: string[];
  symbolResults: SymbolHit[];
  mentionListingCap: MentionListingCap | null;
  selectedFileIndex: number;
  codegraphStatus: string | null;
  slashSearch: string | null;
  selectedSlashIndex: number;
  allSlashCommands: SlashCommand[];
  attachedImages: AttachedImage[];
  isDragOver: boolean;
  uploadError: string | null;
  onSetWikiPrepared: (v: { pages: any[]; autoIngested: boolean } | null) => void;
  onSetMemoryProposals: (
    updater: MemoryProposal[] | ((prev: MemoryProposal[]) => MemoryProposal[]),
  ) => void;
  onSetDistillNotice: (
    v: string | null | ((cur: string | null) => string | null),
  ) => void;
  onSetMsgQueue: (
    updater: MsgQueueItem[] | ((prev: MsgQueueItem[]) => MsgQueueItem[]),
  ) => void;
  onSetInput: (v: string) => void;
  onSetAuto: (updater: boolean | ((prev: boolean) => boolean)) => void;
  onSetPlan: (updater: boolean | ((prev: boolean) => boolean)) => void;
  onSetCanRevertEdit: (v: boolean) => void;
  onSetEditNotice: (v: string | null) => void;
  onSetShowContextPanel: (v: boolean) => void;
  onSetSelectedFileIndex: (v: number) => void;
  onSetSelectedSlashIndex: (v: number) => void;
  onSetAttachedImages: (
    updater: AttachedImage[] | ((prev: AttachedImage[]) => AttachedImage[]),
  ) => void;
  onSetUploadError: (v: string | null) => void;
  onSetLightboxUrl: (v: string | null) => void;
  setSafeTimeout: (fn: () => void, ms: number) => void;
  fetchContextUsage: () => void;
  handleDragStart: (idx: number) => void;
  handleDragOver: (e: React.DragEvent, idx: number) => void;
  handleDragLeave: (idx: number) => void;
  handleDrop: (e: React.DragEvent, idx: number) => void;
  handleDragEnd: () => void;
  moveQueueItem: (index: number, direction: "up" | "down") => void;
  handleQueueClearAll: () => void;
  handleQueueDragStart: (idx: number) => void;
  handleQueueDragOver: (e: React.DragEvent, idx: number) => void;
  handleQueueDragLeave: (idx: number) => void;
  handleQueueDrop: (e: React.DragEvent, idx: number) => void;
  handleQueueDragEnd: () => void;
  handleQueueEdit: (item: ServerQueueItem) => void;
  handleQueueRemove: (id: string) => void;
  handleComposerDragOver: (e: React.DragEvent) => void;
  handleComposerDragLeave: () => void;
  handleComposerDrop: (e: React.DragEvent) => void;
  handleRevertEdit: () => void;
  handleCancelEdit: () => void;
  handleInputChange: (val: string, cursorPosition: number) => void;
  handleKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  handlePaste: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
  insertMention: (fileName: string) => void;
  insertSymbol: (symbolName: string) => void;
  insertSlashCommand: (cmd: string) => void;
  handleQueueAdd: () => void;
  stop: () => void;
  send: () => void;
}) {
  const matchingSlash =
    slashSearch !== null
      ? filterSlashCommands(allSlashCommands, slashSearch)
      : [];

  // Defense in depth: usage is validated upstream (normalizeContextUsage),
  // but a malformed payload must degrade to an empty breakdown, not a crash.
  const contextCategories = Array.isArray(contextUsage?.categories)
    ? contextUsage.categories
    : [];

  return (
    <div className="px-6 pb-3 pt-0.5">
      <div className="max-w-3xl mx-auto">
        {wikiPrepared && wikiPrepared.pages.length > 0 && (
          <div className="mb-2 px-2.5 py-1.5 rounded-lg bg-accent/5 border border-accent/20 flex items-center gap-2 text-[11px] text-txt/85">
            <Share2 size={11} className="text-accent shrink-0" />
            <span className="flex-1">
              Wiki: {wikiPrepared.pages.length} structured page{wikiPrepared.pages.length === 1 ? "" : "s"} ready
              <span className="text-faint"> ({wikiPrepared.pages.map((p: any) => p.kind).filter((v: any, i: number, a: any[]) => a.indexOf(v) === i).join(", ")})</span>
            </span>
            <button
              onClick={async () => {
                const pages = wikiPrepared.pages;
                onSetWikiPrepared(null);
                try {
                  const res = await api.wikiIngestPrepared(pages);
                  const notice = `Wiki: ${res.ingested} page${res.ingested === 1 ? "" : "s"} ingested`;
                  onSetDistillNotice(notice);
                  setSafeTimeout(() => onSetDistillNotice((cur) => (cur === notice ? null : cur)), 6000);
                } catch {
                  onSetDistillNotice("Wiki ingest failed");
                }
              }}
              className="shrink-0 px-2 py-0.5 rounded bg-accent/15 hover:bg-accent/25 text-accent font-medium transition text-[10.5px]"
            >
              Ingest
            </button>
            <button
              onClick={() => onSetWikiPrepared(null)}
              className="shrink-0 text-faint/60 hover:text-muted transition"
              title="dismiss"
            >
              x
            </button>
          </div>
        )}
        {memoryProposals.length > 0 && (
          <div className="mb-2 space-y-1.5">
            {memoryProposals.map((prop) => (
              <div
                key={prop.id}
                className="px-2.5 py-1.5 rounded-lg bg-accent/5 border border-accent/20 flex items-start gap-2 text-[11px] text-txt/85"
              >
                <Brain size={11} className="text-accent shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <div className="text-faint text-[10px] mb-0.5">
                    Save to durable memory?
                    <span className="ml-1 text-muted">({prop.category})</span>
                  </div>
                  <div className="truncate italic">&ldquo;{prop.text}&rdquo;</div>
                </div>
                <button
                  onClick={async () => {
                    onSetMemoryProposals((prev) => prev.filter((p) => p.id !== prop.id));
                    try {
                      const res = await api.memoryProposeAccept(prop.id);
                      if (res.ok) {
                        const notice = "Memory saved";
                        onSetDistillNotice(notice);
                        setSafeTimeout(() => onSetDistillNotice((cur) => (cur === notice ? null : cur)), 4000);
                      }
                    } catch {
                      onSetDistillNotice("Memory save failed");
                    }
                  }}
                  className="shrink-0 px-2 py-0.5 rounded bg-accent/15 hover:bg-accent/25 text-accent font-medium transition text-[10.5px]"
                >
                  Save
                </button>
                <button
                  onClick={async () => {
                    onSetMemoryProposals((prev) => prev.filter((p) => p.id !== prop.id));
                    try {
                      await api.memoryProposeDismiss(prop.id);
                    } catch {
                      /* ignore -- card already dismissed locally */
                    }
                  }}
                  className="shrink-0 px-2 py-0.5 rounded text-faint hover:text-muted transition text-[10.5px]"
                >
                  Skip
                </button>
              </div>
            ))}
          </div>
        )}
        {distillNotice && (
          <div className="mb-2 px-1 flex items-center gap-2 text-[10.5px] text-faint/80">
            <span className="flex-1 truncate">
              {distillNotice}
            </span>
            <button
              onClick={() => onSetDistillNotice(null)}
              className="text-faint/50 hover:text-muted transition shrink-0"
              title="dismiss"
            >
              x
            </button>
          </div>
        )}
        {msgQueue.length > 0 && (
          <div className="mb-3 space-y-1.5">
            <div className="flex items-center justify-between mb-1 px-1">
              <span className="text-[10px] uppercase tracking-wider text-faint font-semibold">
                Queued ({msgQueue.length})
              </span>
              <button
                onClick={() => onSetMsgQueue([])}
                className="text-[10px] text-faint hover:text-muted transition font-semibold"
              >
                Clear all
              </button>
            </div>
            {msgQueue.map((qm, idx) => {
              const isDragOverRow = dragOverIndex === idx;
              const isDragging = dragIndex === idx;

              return (
                <div
                  key={idx}
                  draggable
                  onDragStart={() => handleDragStart(idx)}
                  onDragOver={(e) => handleDragOver(e, idx)}
                  onDragLeave={() => handleDragLeave(idx)}
                  onDrop={(e) => handleDrop(e, idx)}
                  onDragEnd={handleDragEnd}
                  className={`flex items-center justify-between bg-panel2/60 border rounded-lg px-3 py-1.5 text-[12px] text-muted transition-all duration-150 select-none
                    ${isDragging ? "opacity-40" : ""}
                    ${isDragOverRow ? "border-accent/40 bg-accent/5" : "border-edge/60 hover:border-edge2"}`}
                >
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <div className="text-faint hover:text-muted cursor-grab active:cursor-grabbing flex items-center justify-center p-0.5">
                      <GripVertical size={12} />
                    </div>
                    <span className="text-faint text-[10px] font-mono select-none">
                      {idx + 1}
                    </span>
                    <span
                      onClick={() => {
                        onSetInput(qm.text);
                        onSetAuto(qm.auto);
                        onSetPlan(qm.plan || false);
                        onSetMsgQueue((prev) => prev.filter((_, i) => i !== idx));
                        taRef.current?.focus();
                      }}
                      title="Click to edit message"
                      className="truncate max-w-md cursor-pointer hover:text-txt hover:underline transition-colors select-none"
                    >
                      {qm.text}
                    </span>
                    {qm.plan && (
                      <span className="text-[9px] uppercase font-bold px-1.5 py-0.5 bg-accent/15 text-accent rounded whitespace-nowrap">
                        plan
                      </span>
                    )}
                    {qm.auto && (
                      <span className="text-[9px] uppercase font-bold px-1.5 py-0.5 bg-warn/15 text-warn rounded whitespace-nowrap">
                        auto
                      </span>
                    )}
                  </div>

                  <div className="flex items-center gap-1 ml-2 flex-shrink-0">
                    <button
                      onClick={() => moveQueueItem(idx, "up")}
                      disabled={idx === 0}
                      title="Move up"
                      className="p-1 rounded text-faint hover:text-muted hover:bg-panel border border-transparent hover:border-edge/40 disabled:opacity-30 disabled:pointer-events-none transition-all"
                    >
                      <ChevronUp size={12} />
                    </button>
                    <button
                      onClick={() => moveQueueItem(idx, "down")}
                      disabled={idx === msgQueue.length - 1}
                      title="Move down"
                      className="p-1 rounded text-faint hover:text-muted hover:bg-panel border border-transparent hover:border-edge/40 disabled:opacity-30 disabled:pointer-events-none transition-all"
                    >
                      <ChevronDown size={12} />
                    </button>
                    <button
                      onClick={() => {
                        onSetMsgQueue((prev) => prev.filter((_, i) => i !== idx));
                      }}
                      title="Cancel/Remove"
                      className="p-1 rounded text-faint hover:text-risk hover:bg-risk/10 border border-transparent hover:border-risk/20 transition-all"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {/* Server-side PROMPT QUEUE, stacked ABOVE the composer (Cursor-style)
            so the "runs next" items are always visible right over the input.
            These prompts are drained by the backend one full turn at a time. */}
        {queueItems.length > 0 && (
          <div className="mb-2 space-y-1">
            <div className="flex items-center justify-between px-1">
              <span className="text-[10px] uppercase tracking-wider text-faint font-semibold">
                {queueItems.length} queued to send
              </span>
              {queueItems.length >= 2 && (
                <button
                  onClick={handleQueueClearAll}
                  className="text-[10px] text-faint hover:text-muted transition font-semibold"
                >
                  Clear all
                </button>
              )}
            </div>
            {queueItems.map((item, idx) => {
              const isDragging = queueDragIndex === idx;
              const isDragOverQ = queueDragOverIndex === idx;
              return (
                <div
                  key={item.id}
                  draggable
                  onDragStart={() => handleQueueDragStart(idx)}
                  onDragOver={(e) => handleQueueDragOver(e, idx)}
                  onDragLeave={() => handleQueueDragLeave(idx)}
                  onDrop={(e) => handleQueueDrop(e, idx)}
                  onDragEnd={handleQueueDragEnd}
                  className={`flex items-center gap-2 bg-panel2/60 border rounded-lg px-2.5 py-1 text-[11px] text-muted transition-all duration-150 select-none
                    ${isDragging ? "opacity-40" : ""}
                    ${isDragOverQ ? "border-accent/40 bg-accent/5" : "border-edge/60 hover:border-edge2"}`}
                >
                  <div className="text-faint hover:text-muted cursor-grab active:cursor-grabbing flex items-center justify-center shrink-0">
                    <GripVertical size={11} />
                  </div>
                  {idx === 0 && (
                    <span
                      title="Runs next"
                      className="shrink-0 text-[9px] uppercase font-bold px-1 py-0.5 bg-accent/15 text-accent rounded"
                    >
                      next
                    </span>
                  )}
                  <span
                    onClick={() => handleQueueEdit(item)}
                    title={item.text}
                    className="truncate flex-1 min-w-0 cursor-pointer hover:text-txt hover:underline transition-colors"
                  >
                    {item.text}
                  </span>
                  {item.images && item.images.length > 0 && (
                    <span
                      title={`${item.images.length} image attachment(s)`}
                      className="shrink-0 flex items-center gap-0.5 text-[9px] font-semibold px-1 py-0.5 bg-panel border border-edge/60 text-faint rounded"
                    >
                      <ImageIcon size={9} />{item.images.length}
                    </span>
                  )}
                  <button
                    onClick={() => handleQueueRemove(item.id)}
                    title="Remove from queue"
                    className="p-0.5 rounded text-faint hover:text-risk hover:bg-risk/10 border border-transparent hover:border-risk/20 transition-all shrink-0"
                  >
                    <X size={11} />
                  </button>
                </div>
              );
            })}
          </div>
        )}
        {/* compact composer: input + a single tidy control row */}
        <WorkspaceChip />
        <div
          onDragOver={handleComposerDragOver}
          onDragLeave={handleComposerDragLeave}
          onDrop={handleComposerDrop}
          className={`relative bg-panel2/80 border rounded-2xl focus-within:border-edge2 shadow-lg shadow-black/20 transition ${
            isDragOver ? "border-accent ring-1 ring-accent" : "border-edge"
          }`}
        >
          {(editingIndex !== null || canRevertEdit || editNotice) && (
            <div className="flex items-center justify-between gap-2 px-3.5 py-1.5 bg-panel border-b border-edge text-[11.5px] text-accent select-none rounded-t-2xl">
              <span className="flex items-center gap-1.5 min-w-0">
                <Pencil size={11} className="shrink-0" />
                <span className="truncate">
                  {editingIndex !== null
                    ? (editNotice || `Editing message #${editingIndex + 1}`)
                    : (editNotice || "Prior turns set aside")}
                </span>
              </span>
              <span className="flex items-center gap-1 shrink-0">
                {editingIndex !== null && (
                  <button
                    type="button"
                    disabled={editBusy || !input.trim()}
                    onClick={() => send()}
                    className="text-accent hover:text-txt transition font-semibold text-[10px] px-1.5 py-0.5 rounded border border-accent/40 bg-accent/10 hover:bg-accent/20 disabled:opacity-50"
                    title="Wipe back to this message and run the edited prompt"
                  >
                    Resubmit
                  </button>
                )}
                {editingIndex !== null && (
                  <button
                    type="button"
                    disabled={editBusy}
                    onClick={() => handleCancelEdit()}
                    className="text-faint hover:text-muted transition font-medium text-[10px] px-1.5 py-0.5 rounded border border-edge bg-panel2/50 hover:bg-panel2 disabled:opacity-50"
                    title="Restore the conversation from before this edit"
                  >
                    Cancel
                  </button>
                )}
                {editingIndex === null && canRevertEdit && (
                  <button
                    type="button"
                    disabled={editBusy}
                    onClick={() => handleRevertEdit()}
                    className="text-accent hover:text-txt transition font-semibold text-[10px] px-1.5 py-0.5 rounded border border-accent/40 bg-accent/10 hover:bg-accent/20 disabled:opacity-50"
                    title="Restore the conversation from before this edit"
                  >
                    Revert?
                  </button>
                )}
                {editingIndex === null && canRevertEdit && (
                  <button
                    type="button"
                    onClick={() => { onSetCanRevertEdit(false); onSetEditNotice(null); }}
                    className="text-faint hover:text-muted transition font-medium text-[10px] px-1.5 py-0.5 rounded border border-edge bg-panel2/50 hover:bg-panel2"
                  >
                    Dismiss
                  </button>
                )}
              </span>
            </div>
          )}

          {showContextPanel && !contextUsage && (
            <div className="flex items-center justify-between p-3.5 bg-panel border-b border-edge text-[11.5px] select-none rounded-t-2xl animate-in slide-in-from-bottom duration-150">
              <div className="flex items-center gap-2 text-faint">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                <span className="font-semibold text-txt">Context Usage</span>
                <span className="text-muted">loading...</span>
              </div>
              <button onClick={() => onSetShowContextPanel(false)} className="text-faint hover:text-muted transition p-0.5 rounded hover:bg-panel2" title="Close">
                <X size={13} />
              </button>
            </div>
          )}
          {showContextPanel && contextUsage && (
            <div className="flex flex-col p-3.5 bg-panel border-b border-edge text-[11.5px] select-none rounded-t-2xl animate-in slide-in-from-bottom duration-150">
              <div className="flex items-center justify-between font-medium mb-2.5">
                <div className="flex items-center gap-1.5">
                  <span className="font-semibold text-txt">Context Usage</span>
                  <span className="text-[10px] bg-accent/15 text-accent px-1.5 py-0.5 rounded-full font-mono">
                    {contextUsagePercent(contextUsage.total, contextUsage.limit)}% Full
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-faint font-mono text-[11px]">
                    ~{formatTokenK(contextUsage.total)}K / {formatTokenK(contextUsage.limit, 0)}K Tokens
                  </span>
                  <button
                    onClick={() => onSetShowContextPanel(false)}
                    className="text-faint hover:text-muted transition p-0.5 rounded hover:bg-panel2"
                    title="Close"
                  >
                    <ChevronDown size={14} />
                  </button>
                </div>
              </div>

              <div className="w-full h-2 bg-panel2 border border-edge/60 rounded-full overflow-hidden flex mb-3">
                {contextCategories.map((cat, idx) => {
                  if (cat.tokens <= 0) return null;
                  const rawPct = (cat.tokens / contextUsage.limit) * 100;
                  const pct = Number.isFinite(rawPct) ? rawPct : 0;
                  return (
                    <div
                      key={cat.name}
                      className={`${CONTEXT_USAGE_COLORS[idx % CONTEXT_USAGE_COLORS.length]} h-full transition-all duration-300`}
                      style={{ width: `${pct}%` }}
                      title={`${cat.name}: ${formatTokenK(cat.tokens)}K tokens (${Math.round(pct)}%)`}
                    />
                  );
                })}
              </div>

              <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-txt/90">
                {contextCategories.map((cat, idx) => {
                  if (cat.tokens <= 0) return null;
                  return (
                    <div key={cat.name} className="flex items-center justify-between text-[11px] font-mono py-0.5 border-b border-edge/10">
                      <div className="flex items-center gap-1.5 truncate">
                        <span className={`w-2 h-2 rounded-full ${CONTEXT_USAGE_COLORS[idx % CONTEXT_USAGE_COLORS.length]} shrink-0`} />
                        <span className="truncate text-muted">{cat.name}</span>
                      </div>
                      <span className="text-txt font-medium shrink-0">
                        {formatTokenK(cat.tokens)}K
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {mentionSearch !== null && (filteredFiles.length > 0 || symbolResults.length > 0 || mentionListingCap) && (
            <div className="absolute left-2 bottom-full mb-1.5 z-50 max-h-[250px] w-[340px] overflow-y-auto bg-panel border border-edge rounded-xl shadow-2xl py-1">
              {filteredFiles.length > 0 && (
                <>
                  <div className="px-2.5 py-1 text-[10px] uppercase font-bold tracking-wider text-faint border-b border-edge/30 select-none">
                    Files
                  </div>
                  {filteredFiles.map((file, idx) => {
                    const isSelected = idx === selectedFileIndex;
                    return (
                      <div
                        key={file}
                        onClick={() => insertMention(file)}
                        onMouseEnter={() => onSetSelectedFileIndex(idx)}
                        className={`flex items-center gap-2 px-3 py-1.5 text-[11.5px] cursor-pointer transition select-none ${
                          isSelected ? "bg-panel2 text-accent font-medium" : "text-txt/90 hover:bg-panel2/50"
                        }`}
                      >
                        <FileText size={11.5} className="shrink-0 opacity-60" />
                        <span className="truncate flex-1 font-mono">{file}</span>
                      </div>
                    );
                  })}
                </>
              )}

              {symbolResults.length > 0 && (
                <>
                  <div className="px-2.5 py-1 text-[10px] uppercase font-bold tracking-wider text-faint border-b border-edge/30 mt-1 select-none flex items-center justify-between">
                    <span>Symbols</span>
                    {codegraphStatus === "indexing" && (
                      <span className="text-[9px] text-muted normal-case font-normal animate-pulse">indexing...</span>
                    )}
                  </div>
                  {symbolResults.map((sym, idx) => {
                    const globalIdx = filteredFiles.length + idx;
                    const isSelected = globalIdx === selectedFileIndex;
                    return (
                      <div
                        key={`${sym.path}:${sym.line}:${sym.name}`}
                        onClick={() => insertSymbol(sym.name)}
                        onMouseEnter={() => onSetSelectedFileIndex(globalIdx)}
                        className={`flex flex-col gap-0.5 px-3 py-1.5 text-[11.5px] cursor-pointer transition select-none ${
                          isSelected ? "bg-panel2 text-accent" : "text-txt/90 hover:bg-panel2/50"
                        }`}
                      >
                        <div className="flex items-center gap-1.5">
                          <Code size={11.5} className="shrink-0 opacity-60" />
                          <span className="font-mono font-medium truncate flex-1 text-left">{sym.name}</span>
                          <span className="text-[9px] font-mono px-1 py-0.2 bg-edge/30 rounded text-muted shrink-0 lowercase">
                            {sym.kind}
                          </span>
                        </div>
                        <span className="text-[10px] text-muted font-mono truncate pl-5 text-left">
                          {sym.path}:{sym.line}
                        </span>
                      </div>
                    );
                  })}
                </>
              )}

              {filteredFiles.length > 0 && symbolResults.length === 0 && codegraphStatus === "indexing" && (
                <div className="px-3 py-1 text-[10px] text-muted/60 select-none italic text-right">
                  symbols indexing...
                </div>
              )}

              {mentionListingCap && (
                <div className="px-3 py-1.5 text-[10px] text-muted border-t border-edge/20 select-none">
                  {formatMentionListingCapMessage(mentionListingCap)}
                </div>
              )}
            </div>
          )}

          {slashSearch !== null && matchingSlash.length > 0 && (
            <div className="absolute left-2 bottom-full mb-1.5 z-50 max-h-[220px] w-[320px] overflow-y-auto bg-panel border border-edge rounded-xl shadow-2xl py-1">
              <div className="px-2.5 py-1 text-[10px] uppercase font-bold tracking-wider text-faint border-b border-edge/30 select-none">
                Commands
              </div>
              {matchingSlash.map((s, idx) => {
                const isSelected = idx === selectedSlashIndex;
                return (
                  <div
                    key={s.cmd}
                    onClick={() => insertSlashCommand(s.cmd)}
                    onMouseEnter={() => onSetSelectedSlashIndex(idx)}
                    className={`flex flex-col px-3 py-1.5 cursor-pointer transition select-none ${
                      isSelected ? "bg-panel2 text-accent font-medium" : "text-txt/90 hover:bg-panel2/50"
                    }`}
                  >
                    <div className="flex items-center gap-1.5 text-[11.5px] font-mono font-semibold">
                      <span>{s.cmd}</span>
                    </div>
                    <span className="text-[10px] text-muted leading-tight">{s.desc}</span>
                  </div>
                );
              })}
            </div>
          )}

          {attachedImages.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 px-3 pt-2.5">
              {attachedImages.map((img, idx) => (
                <div
                  key={idx}
                  className="relative group/thumb w-[40px] h-[40px] rounded-lg overflow-hidden border border-edge bg-panel/50 select-none animate-in fade-in zoom-in duration-150"
                >
                  <img
                    src={img.previewUrl}
                    alt={img.name}
                    onClick={() => onSetLightboxUrl(img.previewUrl)}
                    className="w-full h-full object-cover cursor-pointer hover:opacity-90 transition-opacity"
                  />
                  <button
                    onClick={() => {
                      onSetAttachedImages((prev) => prev.filter((_, i) => i !== idx));
                      URL.revokeObjectURL(img.previewUrl);
                      onSetUploadError(null);
                    }}
                    className="absolute top-0 right-0 p-0.5 bg-black/60 text-txt hover:text-risk opacity-0 group-hover/thumb:opacity-100 flex items-center justify-center transition rounded-bl"
                    title="Remove image"
                  >
                    <X size={11} />
                  </button>
                </div>
              ))}
              {attachedImages.length > 1 && (
                <span className="text-[10px] text-muted self-center ml-1 select-none font-medium">
                  {attachedImages.length} images
                </span>
              )}
            </div>
          )}

          {uploadError && (
            <div className="text-[11px] text-risk px-3 pt-1">
              {uploadError}
            </div>
          )}

          <textarea ref={taRef} value={input}
            onChange={(e) => handleInputChange(e.target.value, e.target.selectionStart)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={1} placeholder={auto ? "Give the pilot an objective..." : "Message the pilot..."}
            className="w-full bg-transparent px-3 pt-2.5 pb-1 text-[0.8125rem] resize-none focus:outline-none overflow-hidden placeholder:text-faint" />
          <div className="flex items-center gap-1.5 px-3 pb-2">
            <button onClick={() => {
              onSetAuto((a) => {
                const next = !a;
                if (next) onSetPlan(false);
                return next;
              });
            }} title="Autopilot: the pilot plans and executes autonomously (vs. you steering each step)"
              className={`px-1.5 h-[20px] rounded-md text-[10.5px] flex items-center gap-1 transition
                ${auto ? "bg-warn/15 text-warn" : "text-faint hover:text-muted"}`}>
              <Zap size={11} /> Autopilot
            </button>
            <button onClick={() => {
              onSetPlan((p) => {
                const next = !p;
                if (next) onSetAuto(false);
                return next;
              });
            }} title="Plan mode -- get an actionable plan instead of execution (read-only)"
              className={`px-1.5 h-[20px] rounded-md text-[10.5px] flex items-center gap-1 transition
                ${plan ? "bg-accent/15 text-accent" : "text-faint hover:text-muted"}`}>
              <ListChecks size={11} /> Plan
            </button>
            <PilotPicker config={config} />
            <button
              onClick={() => {
                onSetShowContextPanel(!showContextPanel);
                if (!showContextPanel) {
                  fetchContextUsage();
                }
              }}
              title="View context window usage breakdown"
              className={`px-1.5 h-[20px] rounded-md text-[10.5px] font-mono flex items-center gap-1 transition
                ${showContextPanel ? "bg-accent/15 text-accent border border-accent/20" : "text-faint hover:text-muted bg-panel2/40 border border-edge/30 hover:bg-panel2/80"}`}
            >
              <FileText size={11} />
              <span>
                {contextUsage
                  ? `${contextUsagePercent(contextUsage.total, contextUsage.limit)}%`
                  : "Usage"}
              </span>
            </button>
            <div className="flex-1" />
            {input.trim() && (
              <button
                onClick={handleQueueAdd}
                title="Queue: runs after the current turn finishes (same as Cmd/Ctrl+Enter)"
                className="px-2 h-[20px] rounded-md bg-panel2/60 border border-edge/60 text-faint hover:text-muted hover:border-edge2 text-[10.5px] font-medium flex items-center gap-1 transition"
              >
                <ListChecks size={9} />Queue
              </button>
            )}
            {composerBusy
              ? <>
                  <button onClick={stop} className="px-2 h-[20px] rounded-md bg-risk/15 text-risk text-[10.5px] font-medium flex items-center gap-1"><Square size={9} />Stop</button>
                  <button onClick={send} disabled={transcriptStale || (!input.trim() && attachedImages.length === 0)}
                    title="Steer: redirect the current turn now (Enter). Cmd/Ctrl+Enter or Queue = run after this turn finishes."
                    className="px-2.5 h-[20px] rounded-md bg-accent text-black/90 text-[10.5px] font-semibold flex items-center gap-1 hover:brightness-110 disabled:opacity-40 disabled:cursor-default transition">
                    <Send size={9} />Steer</button>
                </>
              : <button onClick={send} disabled={transcriptStale || (!input.trim() && attachedImages.length === 0)}
                  className="px-2.5 h-[20px] rounded-md bg-accent text-black/90 text-[10.5px] font-semibold flex items-center gap-1 hover:brightness-110 disabled:opacity-40 disabled:cursor-default transition">
                  <Send size={9} />{auto ? "Run" : plan ? "Plan" : "Send"}</button>}
          </div>
        </div>

      </div>
    </div>
  );
}
