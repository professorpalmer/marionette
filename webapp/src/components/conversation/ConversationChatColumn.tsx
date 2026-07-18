/**
 * Chat-mode column: scrollable transcript feed + composer dock.
 * Conversation owns all state; this is a presentational peel.
 */

import type { ReactNode, RefObject } from "react";
import { panelOpacityClass } from "../../lib/panelTransition";
import {
  TranscriptList,
  type Card,
  type CommandApprovalItem,
  type Item,
} from "../TranscriptList";
import TranscriptEmptyState from "./TranscriptEmptyState";

export default function ConversationChatColumn({
  feedRef,
  transcriptStale,
  items,
  status,
  compactingStatus,
  editingIndex,
  auto,
  plan,
  busyElapsedMs,
  turnOpen,
  onEditMessage,
  onExecuteSend,
  onImageClick,
  onSetCard,
  onExecutePlan,
  onCommandApproval,
  composerDock,
}: {
  feedRef: RefObject<HTMLDivElement | null>;
  transcriptStale: boolean;
  items: Item[];
  status: "idle" | "thinking" | "executing" | "done" | "error" | "streaming";
  compactingStatus: string | null;
  editingIndex: number | null;
  auto: boolean;
  plan: boolean;
  busyElapsedMs: number | null;
  turnOpen: boolean;
  onEditMessage: (idx: number, text: string) => void;
  onExecuteSend: (msg: string, useAuto: boolean, usePlan?: boolean) => void;
  onImageClick: (url: string) => void;
  onSetCard: (id: string, patch: Partial<Card>) => void;
  onExecutePlan: (planText: string) => void;
  onCommandApproval: (item: CommandApprovalItem, approve: boolean) => void;
  composerDock: ReactNode;
}) {
  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div ref={feedRef} className={`flex-1 overflow-y-auto ${panelOpacityClass(transcriptStale)}`}>
        <div className="max-w-3xl mx-auto px-6 py-6 flex flex-col gap-1">
          <TranscriptEmptyState transcriptStale={transcriptStale} itemCount={items.length} />
          {/*
            PERF: The transcript is rendered by TranscriptList, a React.memo
            component whose props are deliberately independent of the composer
            `input` state. Because typing only mutates `input` (which lives in
            this parent) and none of TranscriptList's props change per keystroke,
            React skips re-rendering the transcript on every keystroke. This
            breaks the old coupling where items.map ran on the ENTIRE transcript
            for each character typed (cost grew with message count).
          */}
          <TranscriptList
            items={items}
            status={status}
            compactingStatus={compactingStatus}
            editingIndex={editingIndex}
            auto={auto}
            plan={plan}
            busyElapsedMs={busyElapsedMs}
            turnOpen={turnOpen}
            scrollContainerRef={feedRef}
            onEditMessage={onEditMessage}
            onExecuteSend={onExecuteSend}
            onImageClick={onImageClick}
            onSetCard={onSetCard}
            onExecutePlan={onExecutePlan}
            onCommandApproval={onCommandApproval}
          />
        </div>
      </div>
      {composerDock}
    </div>
  );
}
