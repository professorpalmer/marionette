export {
  resolveSwitchTranscript,
  clearTranscriptCache,
  peekTranscriptCache,
  peekTranscriptCacheEntry,
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
  mergeSwarmResultReuse,
} from "./transcriptItems";
export {
  finalizeStreamingThinking,
  upsertStreamingThinking,
  type ToolPrepOpts,
  upsertToolPrep,
  clearToolPrepPlaceholders,
  newThinkingId,
  coalesceThinkingChunk,
  looksLikeFinalAnswer,
  hoistCardsBeforeTrailingFinals,
  isTrivialAssistantCrumb,
  sealStreamById,
} from "./thinkingToolPrep";
export {
  nextAppliedCursor,
  isTerminalStreamKind,
  shouldPollChatEvents,
  isChatEventsReattachArmed,
  shouldArmChatEventsFromRunners,
  type ChatEventReplayMissFields,
  isChatEventReplayMiss,
  shouldAdvanceReplayCursor,
  ringGenerationAfterReplayMiss,
  shouldHydrateTranscriptOnReplayMiss,
  cursorAfterReplayMiss,
  shouldRetryRingAfterReplayMiss,
  shouldApplyReattachFrame,
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
  statusPillClickable,
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
  compactionAbortLabel,
  truncateWaitHint,
  shouldPaintThinking,
  sealOpenStreamSurfaces,
  ensureAssistantStreamingBubble,
  ensureWorkerStreamingBubble,
  finalizePilotMessage,
  appendActionStartCard,
  applyActionResultCard,
  isDurableTerminalActionResult,
  isUpgradeableActionResult,
  mergeJobActionsIntoItems,
  foldSwarmLiveJobsAfterReload,
  shouldApplySwarmLiveMerge,
  reconcileTerminalJobCards,
  reconcileOrphanInvestigationCards,
  finalizeStreamingBubbleOnActionResult,
  workspaceRootFromActionResult,
  appendSwarmPending,
  appendCheckpoint,
  appendPendingReview,
  focusReviewTabAndRefresh,
  swarmResultOutcome,
  appendQueuedPromptUserBubble,
  appendAutoHalt,
  appendAutoStatus,
  appendAutoVerify,
  appendQualityGate,
  appendVerification,
  appendVerifying,
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
  SESSION_STATE_FAIL_NOTICE,
  SESSION_TRANSCRIPT_FAIL_NOTICE,
  clearRecoveredSessionFailNotice,
  shouldResetBusyChromeOnSwitch,
  sessionStateFailureSwitchDecision,
  shouldRetryEmptyTranscript,
  cacheHitEmptyTranscriptDecision,
  emptyTranscriptAfterRetryDecision,
  transcriptRefreshFailureDecision,
  reattachSessionStateFailureDecision,
} from "./sessionHydrate";
export {
  clearComposerDraftCache,
  peekComposerDraft,
  writeComposerDraft,
  resolveComposerDraftOnSwitch,
} from "./composerDraftCache";
export {
  clearComposerAttachmentCache,
  peekComposerAttachments,
  writeComposerAttachments,
  resolveComposerAttachmentsOnSwitch,
  releaseDroppedComposerAttachmentPreviews,
  type ComposerAttachedImage,
} from "./composerAttachmentCache";
export {
  composerEnterAction,
  composerEnterBusy,
  executeSendGate,
  shouldBlockEmptySend,
  shouldSteerWhileBusy,
  formatHelpSlashReply,
  formatCompactCompleteMessage,
  formatCompactErrorMessage,
  shouldApplyCompactSettle,
  formatSteerErrorMessage,
  formatInterruptErrorMessage,
  shouldClearSteerDraftOnResult,
  formatRenderCommandErrorMessage,
  editNoticeAfterSend,
  EDIT_BUSY_PROGRESS_NOTICE,
  STOP_INTERRUPT_FAILED_NOTICE,
  userOrdinalBeforeIndex,
  showStandaloneEditNoticeDismiss,
  runEditMessageFlow,
  runStopFlow,
  classifyLocalSlashCommand,
  localSlashChromeAction,
  localSlashPaletteAction,
} from "./composerSend";
export {
  detectComposerTrigger,
  quoteMentionPathIfNeeded,
  formatMentionToken,
  buildMentionInsert,
  buildSymbolInsert,
  buildFolderInsert,
  buildCodebaseInsert,
  codebaseMentionMatches,
  codebaseQueryFromMentionSearch,
  filterMentionPaths,
  filterSlashCommands,
  cycleSelectIndex,
  mentionTokenForDroppedPath,
  appendMentionsToInput,
  clampSelectIndex,
} from "./composerInput";
export {
  moveItem,
  reorderByDrag,
  blankQueueItemsOnSessionSwitch,
  blankMsgQueueOnSessionSwitch,
  shouldApplyQueueRefresh,
  QUEUE_LOAD_FAIL_NOTICE,
} from "./queueOps";
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
  staleLocalStreamTickDecision,
  RUNNERS_IDLE_CONFIRM_POLLS,
  isAgentLoopOpen,
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
export {
  classifySwarmPollEvent,
  appendMemoryProposal,
  SWARM_AWAIT_HINT,
  PILOT_LOOKING_HINT,
  hasLiveBackgroundJobIds,
  shouldHoldSwarmAwaitChrome,
  sessionStateShowsAwaitingSwarm,
  seedPendingJobIdsFromHydrate,
  hydratePendingJobIdsAfterReload,
  pendingJobIdsFromSwarmLive,
  waitHintForAssistantDone,
  clearSwarmAwaitWaitHint,
  isSwarmAwaitWaitHint,
  pruneTerminalJobIds,
  terminalJobIdsFromSwarmLive,
  swarmResultsAwaitChromeClear,
  pilotResumePollAction,
  triggerResumeGate,
} from "./swarmPoll";
export { armResumeKick, scheduleResumeIfPending } from "./sessionResumeLatch";
export {
  pumpTypewriterFrame,
  startTypewriterLoop,
  flushTypewriterBuffer,
  cancelTypewriterWithoutFlush,
} from "./streamTypewriter";

export {
  FEED_PIN_THRESHOLD_PX,
  FEED_REPIN_THRESHOLD_PX,
  FEED_SETTLE_TIMEOUT_MS,
  FEED_UNPIN_BUBBLE_EVENT,
  feedWheelUnpinListenerOptions,
  isPinnedToBottom,
  nextFeedPinState,
  pinStateFromScrollGeometry,
  shouldStopNestedWheelBubble,
  shouldUnpinInnerOnWheel,
  shouldUnpinOnWheel,
  shouldUnpinOnTouchMove,
  settleFrameResult,
  THINKING_INNER_PIN_THRESHOLD_PX,
} from "./feedScroll";
export {
  STREAM_ABORT_MESSAGE,
  streamErrorText,
  streamOnDoneDecision,
  streamOnErrorDecision,
  shouldRefreshBusyChrome,
  resetTurnSettledOnSessionSwitch,
  resetCrossSessionLatchesOnSwitch,
} from "./streamTerminal";
export { default as TranscriptEmptyState } from "./TranscriptEmptyState";
export { createChatEventsReattach } from "./chatEventsReattach";
export { gatherSessionArtifacts } from "./sessionArtifacts";
export { useSessionSwitch } from "./useSessionSwitch";
export { useRunnersBusyPoll } from "./useRunnersBusyPoll";
export { default as ConversationChatColumn } from "./ConversationChatColumn";
