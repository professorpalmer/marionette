export {
  resolveSwitchTranscript,
  clearTranscriptCache,
  peekTranscriptCache,
  writeTranscriptCache,
} from "./transcriptCache";
export {
  getSimilarity,
  deduplicateAssistantNarration,
  dedupeDisplayItems,
  stripUserVisibleText,
  transcriptResponseToItems,
  shouldPreferLocalTranscript,
  mergeTranscriptItems,
  transcriptFingerprint,
} from "./transcriptItems";
export {
  finalizeStreamingThinking,
  upsertStreamingThinking,
  type ToolPrepOpts,
  upsertToolPrep,
  clearToolPrepPlaceholders,
  newThinkingId,
  looksLikeFinalAnswer,
  hoistCardsBeforeTrailingFinals,
  isTrivialAssistantCrumb,
} from "./thinkingToolPrep";
export {
  nextAppliedCursor,
  isTerminalStreamKind,
  shouldPollChatEvents,
  shouldArmChatEventsFromRunners,
  type ChatEventReplayMissFields,
  isChatEventReplayMiss,
  shouldAdvanceReplayCursor,
  ringGenerationAfterReplayMiss,
  shouldHydrateTranscriptOnReplayMiss,
  cursorAfterReplayMiss,
  shouldRetryRingAfterReplayMiss,
  chatFrameToStreamEvent,
} from "./chatEvents";
export {
  isWorkspaceOpenLeaseExhausted,
  formatWorkspaceOpenLeaseExhaustedMessage,
} from "./leaseExhausted";
export { composerStatusFromRunner } from "./composerStatus";
export {
  SLASH_COMMANDS,
  formatMentionListingCapMessage,
  mergeSlashCommands,
  isBuiltInSlashCommand,
} from "./slashCommands";
export {
  normalizeTabPath,
  pathIsUnder,
  filterTabsAfterDelete,
  remapTabsAfterRename,
  remapActiveTabAfterRename,
} from "./tabPaths";
export {
  findStreamingBubbleIdx,
  appendStreamingTextToItems,
  finalizeOpenPilotBubble,
  typewriterCharsPerFrame,
  assistantProseCovers,
  sealedAssistantTextsInTurn,
  sealedAssistantCoversDelta,
  PROSE_COVER_MIN_CHUNK,
} from "./streamBubbles";
export { derivePillStatus } from "./pillStatus";
export { workspaceLeafName } from "./workspaceDisplay";
export {
  statusPillLabel,
  statusPillTextClass,
  statusPillDotClass,
} from "./StatusPill";
export { default as StatusPill } from "./StatusPill";
export { default as WorkspaceChip } from "./WorkspaceChip";
export {
  patchCardInItems,
  appendAuthFailure,
  appendCommandBlocked,
  appendCodegraphContext,
  appendCompaction,
  truncateWaitHint,
  shouldPaintThinking,
  sealOpenStreamSurfaces,
  ensureAssistantStreamingBubble,
  ensureWorkerStreamingBubble,
  finalizePilotMessage,
  appendActionStartCard,
  applyActionResultCard,
  mergeJobActionsIntoItems,
  foldSwarmLiveJobsAfterReload,
  shouldApplySwarmLiveMerge,
  reconcileTerminalJobCards,
  reconcileOrphanInvestigationCards,
  finalizeStreamingBubbleOnActionResult,
  workspaceRootFromActionResult,
  appendSwarmPending,
  appendCheckpoint,
  appendQueuedPromptUserBubble,
  appendAutoHalt,
  appendAutoStatus,
  appendStreamError,
  appendNonStreamingThinking,
  applySwarmResultToItems,
  failSwarmPendingForActionError,
  finalizeOrphanSwarmPills,
  swarmPendingStatus,
  formatDistilledNotice,
  formatWikiAutoIngestNotice,
} from "./streamApply";
export {
  normalizeSwarmJobIds,
  swarmPendingIdentityKey,
  mergeSwarmPendingItems,
} from "./swarmPendingIdentity";
export {
  collectDisplayArtifacts,
  mergeUniqueArtifacts,
  emptySessionSwitchState,
  shouldPreserveBusyStatus,
  runnerBusySwitchDecision,
} from "./sessionHydrate";
export {
  composerEnterAction,
  executeSendGate,
  shouldBlockEmptySend,
  formatHelpSlashReply,
  formatCompactCompleteMessage,
  formatCompactErrorMessage,
  formatSteerErrorMessage,
  formatRenderCommandErrorMessage,
  editNoticeAfterSend,
  classifyLocalSlashCommand,
} from "./composerSend";
export {
  detectComposerTrigger,
  buildMentionInsert,
  buildSymbolInsert,
  filterSlashCommands,
  cycleSelectIndex,
  mentionTokenForDroppedPath,
  appendMentionsToInput,
  clampSelectIndex,
} from "./composerInput";
export { moveItem, reorderByDrag } from "./queueOps";
export {
  notifyPrefEnabled,
  soundPrefEnabled,
  queueMessagesPrefEnabled,
  shouldShowCompletionNotification,
} from "./completionNotify";
export { createApplyStreamEvent } from "./streamEventHandler";
export {
  upsertOpenTab,
  closeTabResult,
  setTabDirty,
  tabHasDirty,
  otherTabsHaveDirty,
} from "./openFileTabs";
export {
  userStoppedBusyChrome,
  preserveOrThinking,
  runnersBusyTickDecision,
  RUNNERS_IDLE_CONFIRM_POLLS,
} from "./runnersBusy";
export {
  CONTEXT_USAGE_COLORS,
  contextUsagePercent,
  formatTokenK,
  normalizeContextUsage,
} from "./contextUsageColors";
export { default as EditorTabStrip } from "./EditorTabStrip";
export { default as ComposerDock } from "./ComposerDock";
export { default as ConversationHeader } from "./ConversationHeader";
export { default as ImageLightbox } from "./ImageLightbox";
export { classifySwarmPollEvent, appendMemoryProposal } from "./swarmPoll";
export {
  pumpTypewriterFrame,
  startTypewriterLoop,
  flushTypewriterBuffer,
  cancelTypewriterWithoutFlush,
} from "./streamTypewriter";

export {
  FEED_PIN_THRESHOLD_PX,
  FEED_SETTLE_TIMEOUT_MS,
  isPinnedToBottom,
  pinStateFromScrollGeometry,
  shouldUnpinOnWheel,
  shouldUnpinOnTouchMove,
  settleFrameResult,
} from "./feedScroll";
export {
  STREAM_ABORT_MESSAGE,
  streamErrorText,
  streamOnDoneDecision,
  streamOnErrorDecision,
} from "./streamTerminal";
export { default as TranscriptEmptyState } from "./TranscriptEmptyState";
export { createChatEventsReattach } from "./chatEventsReattach";
export { gatherSessionArtifacts } from "./sessionArtifacts";
export { useSessionSwitch } from "./useSessionSwitch";
export { useRunnersBusyPoll } from "./useRunnersBusyPoll";
export { default as ConversationChatColumn } from "./ConversationChatColumn";
