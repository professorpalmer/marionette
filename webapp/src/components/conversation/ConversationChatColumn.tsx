import type { TranscriptViewportHandle } from "./sessionViewport";
/**
 * Chat-mode column: scrollable transcript feed + composer dock.
 * Conversation owns all state; this is a presentational peel.
 *
 * Last N session transcripts stay mounted (hidden) so a click back does not
 * teardown markdown / the virtualizer. Live `items` bind only to the session
 * that currently owns them — never paint session A rows under B's id.
 */

import { type MutableRefObject, type ReactNode, type RefObject } from "react";
import { ChevronDown } from "lucide-react";
import { panelOpacityClass } from "../../lib/panelTransition";
import {
  TranscriptList,
  countPaintableTranscriptItems,
  type Card,
  type CommandApprovalItem,
  type SecretRequestItem,
  type Item,
} from "../TranscriptList";
import TranscriptEmptyState from "./TranscriptEmptyState";
import {
  feedContentLayoutClass,
  feedLiveStreamOpen,
  feedScrollportStyle,
  feedSeatingReservePx,
} from "./feedScroll";
import { peekTranscriptCache } from "./transcriptCache";
import { retainSessionPanes } from "./sessionPanes";

const DORMANT_SCROLL_REF: RefObject<HTMLDivElement | null> = { current: null };

type SessionStatus =
  | "idle"
  | "thinking"
  | "executing"
  | "done"
  | "error"
  | "streaming"
  | "awaiting_swarm";

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
  modelLabel = "",
  waitHint = null,
  providerElapsedMs = null,
  turnOpen,
  holdSwarmAwait = false,
  feedSettled = true,
  scrollToEndRef,
  viewportRef,
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
  sessionId,
  itemSessionId = "",
  paneIds = [],
}: {
  feedRef: RefObject<HTMLDivElement | null>;
  /** Direct child of the feed scrollport — observed for height-driven stick. */
  feedContentRef?: RefObject<HTMLDivElement | null>;
  transcriptStale: boolean;
  items: Item[];
  status: SessionStatus;
  compactingStatus: string | null;
  editingIndex: number | null;
  auto: boolean;
  plan: boolean;
  busyElapsedMs: number | null;
  modelLabel?: string | null;
  waitHint?: string | null;
  providerElapsedMs?: number | null;
  turnOpen: boolean;
  /** Same hold as Conversation — pending jobs keep transcript latch through idle flaps. */
  holdSwarmAwait?: boolean;
  /** Defer DOM row measurement while session-switch settle glue runs. */
  feedSettled?: boolean;
  scrollToEndRef?: MutableRefObject<(() => void) | null>;
  viewportRef?: MutableRefObject<TranscriptViewportHandle | null>;
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
  sessionId?: string;
  /** Session that currently owns `items` (may lag activeSessionId by one frame). */
  itemSessionId?: string;
  paneIds?: string[];
}) {
  const ids = retainSessionPanes({
    prev: paneIds,
    activeId: sessionId,
  });
  const paneList = ids.length > 0 ? ids : [sessionId || ""];
  const paintCount = countPaintableTranscriptItems(items);
  const seatingReservePx = feedSeatingReservePx({
    liveStreamOpen: feedLiveStreamOpen(status, turnOpen),
  });
  // Dim only when stale rows are on screen (refresh flake). Empty cold-miss
  // dim was the swap blink — nothing to honesty-dim.
  const feedDimmed = transcriptStale && paintCount > 0;
  return (
    <div
      className="chat-column flex flex-col flex-1 min-h-0 min-w-0"
    >
      <div className="relative flex-1 min-h-0 flex flex-col">
        {paneList.map((id) => {
          const visible = !id || id === sessionId;
          const live = id === itemSessionId;
          const paneItems = live ? items : (peekTranscriptCache(id) || []);
          const panePaint = live ? paintCount : countPaintableTranscriptItems(paneItems);
          return (
            <div
              key={id}
              ref={visible ? feedRef : undefined}
              data-testid={visible ? "transcript-feed-scrollport" : undefined}
              data-session-pane={id}
              aria-hidden={!visible}
              inert={!visible || undefined}
              aria-busy={visible && transcriptStale && paintCount === 0 ? true : undefined}
              className={
                visible
                  ? `flex-1 min-h-0 overflow-y-auto overscroll-contain [scrollbar-gutter:stable] ${panelOpacityClass(false, feedDimmed)}`
                  : "absolute inset-0 overflow-y-auto overscroll-contain invisible pointer-events-none [scrollbar-gutter:stable]"
              }
              style={feedScrollportStyle()}
            >
              <div
                ref={visible ? feedContentRef : undefined}
                data-testid={visible ? "transcript-feed-content" : undefined}
                className={feedContentLayoutClass()}
                style={{ paddingBottom: seatingReservePx }}
              >
                {visible ? (
                  <TranscriptEmptyState
                    transcriptStale={live ? transcriptStale : panePaint === 0}
                    itemCount={panePaint}
                  />
                ) : null}
                <TranscriptList
                  items={paneItems}
                  status={live ? status : "idle"}
                  compactingStatus={live ? compactingStatus : null}
                  editingIndex={live ? editingIndex : null}
                  auto={auto}
                  plan={plan}
                  busyElapsedMs={live ? busyElapsedMs : null}
                  modelLabel={live ? modelLabel : ""}
                  waitHint={live ? waitHint : null}
                  providerElapsedMs={live ? providerElapsedMs : null}
                  turnOpen={live ? turnOpen : false}
                  holdSwarmAwait={live ? holdSwarmAwait : false}
                  feedSettled={live ? feedSettled : true}
                  scrollContainerRef={visible ? feedRef : DORMANT_SCROLL_REF}
                  scrollToEndRef={visible ? scrollToEndRef : undefined}
                  viewportRef={visible ? viewportRef : undefined}
                  onEditMessage={onEditMessage}
                  onExecuteSend={onExecuteSend}
                  onImageClick={onImageClick}
                  onSetCard={onSetCard}
                  onExecutePlan={onExecutePlan}
                  onCommandApproval={onCommandApproval}
                  onSecretRequest={onSecretRequest}
                  onAuthFailureRetry={onAuthFailureRetry}
                  sessionId={id}
                />
              </div>
            </div>
          );
        })}
      {showJumpToBottom ? (
        <button
          type="button"
          data-testid="jump-to-latest"
          title="Jump to latest"
          aria-label="Jump to latest"
          onClick={onJumpToBottom}
          className="transcript-fold-chrome select-none absolute bottom-3 left-1/2 -translate-x-1/2 z-10 flex items-center justify-center w-8 h-8 rounded-full border border-edge2 text-muted hover:text-txt hover:bg-panel2/80 transition-colors"
          style={{ backgroundColor: "#0f1113" }}
        >
          <ChevronDown size={16} />
        </button>
      ) : null}
      </div>
      <div className="transcript-fold-chrome select-none shrink-0 min-w-0" data-testid="composer-chrome">
        {composerDock}
      </div>
    </div>
  );
}
