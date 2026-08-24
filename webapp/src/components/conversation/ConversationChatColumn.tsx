/**
 * Chat-mode column: scrollable transcript feed + composer dock.
 * Conversation owns all state; this is a presentational peel.
 */

import { useLayoutEffect, useRef, useState, type CSSProperties, type MutableRefObject, type ReactNode, type RefObject } from "react";
import { motion } from "motion/react";
import { ChevronDown } from "lucide-react";
import { panelOpacityClass } from "../../lib/panelTransition";
import { feedBottomClearancePx, FEED_CHROME_CLEARANCE_VAR } from "./feedScroll";
import {
  TranscriptList,
  type Card,
  type CommandApprovalItem,
  type SecretRequestItem,
  type Item,
} from "../TranscriptList";
import TranscriptEmptyState from "./TranscriptEmptyState";
import { FeedOverlayHost } from "../../lib/overlayPortal";

export default function ConversationChatColumn({
  feedRef,
  feedContentRef,
  transcriptStale,
  items,
  status,
  compactingStatus,
  editingIndex,
  auto,
  plan,
  busyElapsedMs,
  turnOpen,
  holdSwarmAwait = false,
  feedSettled = true,
  scrollToEndRef,
  onEditMessage,
  onExecuteSend,
  onImageClick,
  onSetCard,
  onExecutePlan,
  onCommandApproval,
  onSecretRequest,
  onAuthFailureRetry,
  composerDock,
  showJumpToBottom = false,
  onJumpToBottom,
}: {
  feedRef: RefObject<HTMLDivElement | null>;
  /** Direct child of the feed scrollport — observed for height-driven stick. */
  feedContentRef?: RefObject<HTMLDivElement | null>;
  transcriptStale: boolean;
  items: Item[];
  status: "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | "awaiting_swarm";
  compactingStatus: string | null;
  editingIndex: number | null;
  auto: boolean;
  plan: boolean;
  busyElapsedMs: number | null;
  turnOpen: boolean;
  /** Same hold as Conversation — pending jobs keep transcript latch through idle flaps. */
  holdSwarmAwait?: boolean;
  /** Defer DOM row measurement while session-switch settle glue runs. */
  feedSettled?: boolean;
  scrollToEndRef?: MutableRefObject<(() => void) | null>;
  onEditMessage: (idx: number, text: string) => void;
  onExecuteSend: (msg: string, useAuto: boolean, usePlan?: boolean) => void;
  onImageClick: (url: string) => void;
  onSetCard: (id: string, patch: Partial<Card>) => void;
  onExecutePlan: (planText: string) => void;
  onCommandApproval: (item: CommandApprovalItem, decision: boolean | "amendment") => void;
  onSecretRequest?: (item: SecretRequestItem, decision: { action: "save"; value: string } | { action: "dismiss" }) => void;
  onAuthFailureRetry?: () => void;
  composerDock: ReactNode;
  showJumpToBottom?: boolean;
  onJumpToBottom?: () => void;
}) {
  const chromeRef = useRef<HTMLDivElement>(null);
  const [clearancePx, setClearancePx] = useState(96);
  useLayoutEffect(() => {
    const node = chromeRef.current;
    if (!node) return;
    const sync = () => {
      setClearancePx(feedBottomClearancePx(node.getBoundingClientRect().height));
    };
    sync();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(sync) : null;
    ro?.observe(node);
    return () => ro?.disconnect();
  }, []);

  return (
    <div
      className="chat-column flex flex-col flex-1 min-h-0 min-w-0"
      style={{ [FEED_CHROME_CLEARANCE_VAR]: `${clearancePx}px` } as CSSProperties}
    >
      <div className="relative flex-1 min-h-0 flex flex-col">
        <motion.div
          ref={feedRef}
          layoutScroll
          className={`flex-1 min-h-0 overflow-y-auto overscroll-contain [overflow-anchor:auto] [scrollbar-gutter:stable] [scroll-padding-bottom:var(--feed-chrome-clearance,clamp(72px,12vh,144px))] ${panelOpacityClass(transcriptStale)}`}
        >
        {/* overflow-anchor:auto — browser tail anchoring during growth; scroll-padding-bottom
            tracks composer chrome via --feed-chrome-clearance (ResizeObserver). nextFeedPinState
            hysteresis still owns stick/unstick. scrollbar-gutter avoids a 15px jump when the bar
            appears. overscroll-contain stops rubber-band from yanking the window.
            Composer sits outside this scrollport; do not move it inside. */}
        <div
          ref={feedContentRef}
          className="max-w-3xl mx-auto px-6 py-6 flex flex-col gap-1"
        >
          <TranscriptEmptyState transcriptStale={transcriptStale} itemCount={items.length} />
          {/*
            PERF: The transcript is rendered by TranscriptList, a React.memo
            component whose props are deliberately independent of the composer
            `input` state. Because typing only mutates `input` (which lives in
            this parent) and none of TranscriptList's props change per keystroke,
            React skips re-rendering the transcript on every keystroke. This
            breaks the old coupling where items.map ran on the ENTIRE transcript
            for each character typed (cost grew with message count). Row mounting
            is further bounded by @tanstack/react-virtual inside TranscriptList.
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
            holdSwarmAwait={holdSwarmAwait}
            feedSettled={feedSettled}
            scrollContainerRef={feedRef}
            scrollToEndRef={scrollToEndRef}
            onEditMessage={onEditMessage}
            onExecuteSend={onExecuteSend}
            onImageClick={onImageClick}
            onSetCard={onSetCard}
            onExecutePlan={onExecutePlan}
            onCommandApproval={onCommandApproval}
            onSecretRequest={onSecretRequest}
            onAuthFailureRetry={onAuthFailureRetry}
          />
        </div>
      </motion.div>
      <FeedOverlayHost />
      {showJumpToBottom ? (
        <button
          type="button"
          data-testid="jump-to-latest"
          title="Jump to latest"
          aria-label="Jump to latest"
          onClick={onJumpToBottom}
          className="absolute bottom-3 left-1/2 -translate-x-1/2 z-10 flex items-center justify-center w-8 h-8 rounded-full border border-edge2 text-muted hover:text-txt hover:bg-panel2/80 transition-colors"
          style={{ backgroundColor: "#0f1113" }}
        >
          <ChevronDown size={16} />
        </button>
      ) : null}
      </div>
      <div ref={chromeRef} className="shrink-0 min-w-0" data-testid="composer-chrome">
        {composerDock}
      </div>
    </div>
  );
}
