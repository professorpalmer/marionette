/**
 * Continue / Retry: disabled while busy, one dispatch, honest visible send.
 */
import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ComposerDock from "../components/conversation/ComposerDock";

vi.mock("../lib/api", () => ({
  api: {},
}));
vi.mock("../components/PilotPicker", () => ({
  default: () => <div data-testid="pilot-picker" />,
}));
vi.mock("../components/SwarmReasoningPicker", () => ({
  default: () => <div data-testid="swarm-reasoning-picker" />,
}));
vi.mock("../components/conversation/WorkspaceChip", () => ({
  default: () => <div data-testid="workspace-chip" />,
}));

const noop = () => {};

function renderDock(opts: {
  composerBusy: boolean;
  recoveryAvailable?: boolean;
  recoveryRetryAvailable?: boolean;
  onContinue?: () => void;
  onRetry?: () => void;
}) {
  return render(
    <ComposerDock
      config={null}
      taRef={createRef<HTMLTextAreaElement>()}
      input=""
      auto={false}
      plan={false}
      composerBusy={opts.composerBusy}
      transcriptStale={false}
      wikiPrepared={null}
      memoryProposals={[]}
      distillNotice={null}
      msgQueue={[]}
      dragIndex={null}
      dragOverIndex={null}
      queueItems={[]}
      queueDragIndex={null}
      queueDragOverIndex={null}
      editingIndex={null}
      canRevertEdit={false}
      editNotice={null}
      editBusy={false}
      showContextPanel={false}
      contextUsage={null}
      mentionSearch={null}
      filteredFiles={[]}
      filteredFolders={[]}
      symbolResults={[]}
      mentionListingCap={null}
      selectedFileIndex={0}
      codegraphStatus={null}
      slashSearch={null}
      selectedSlashIndex={0}
      allSlashCommands={[]}
      attachedImages={[]}
      isDragOver={false}
      uploadError={null}
      onSetWikiPrepared={noop}
      onSetMemoryProposals={noop}
      onSetDistillNotice={noop}
      onSetMsgQueue={noop}
      onSetInput={noop}
      onSetAuto={noop}
      onSetPlan={noop}
      onSetCanRevertEdit={noop}
      onSetEditNotice={noop}
      onSetShowContextPanel={noop}
      onSetSelectedFileIndex={noop}
      onSetSelectedSlashIndex={noop}
      onSetAttachedImages={noop}
      onSetUploadError={noop}
      onSetLightboxUrl={noop}
      setSafeTimeout={noop}
      fetchContextUsage={noop}
      handleDragStart={noop}
      handleDragOver={noop}
      handleDragLeave={noop}
      handleDrop={noop}
      handleDragEnd={noop}
      moveQueueItem={noop}
      handleQueueClearAll={noop}
      handleQueueDragStart={noop}
      handleQueueDragOver={noop}
      handleQueueDragLeave={noop}
      handleQueueDrop={noop}
      handleQueueDragEnd={noop}
      handleQueueEdit={noop}
      handleQueueRemove={noop}
      handleComposerDragOver={noop}
      handleComposerDragLeave={noop}
      handleComposerDrop={noop}
      handleRevertEdit={noop}
      handleCancelEdit={noop}
      handleInputChange={noop}
      handleKeyDown={noop}
      handlePaste={noop}
      insertMention={noop}
      insertFolder={noop}
      insertSymbol={noop}
      insertCodebase={noop}
      showCodebaseMention={false}
      insertSlashCommand={noop}
      handleQueueAdd={noop}
      stop={noop}
      send={noop}
      recoveryAvailable={opts.recoveryAvailable}
      recoveryRetryAvailable={opts.recoveryRetryAvailable}
      recoveryCause="provider_eof"
      onContinue={opts.onContinue}
      onRetry={opts.onRetry}
    />,
  );
}

describe("ComposerDock recovery chrome", () => {
  it("hides Continue/Retry while the mouth is busy", () => {
    renderDock({ composerBusy: true, recoveryAvailable: true });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });

  it("dispatches Continue and Retry exactly once from an incomplete turn", () => {
    const onContinue = vi.fn();
    const onRetry = vi.fn();
    renderDock({
      composerBusy: false,
      recoveryAvailable: true,
      recoveryRetryAvailable: true,
      onContinue,
      onRetry,
    });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onContinue).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("disables Retry when there is no latest user ask", () => {
    const onRetry = vi.fn();
    renderDock({
      composerBusy: false,
      recoveryAvailable: true,
      recoveryRetryAvailable: false,
      onRetry,
    });
    expect(screen.getByRole("button", { name: /retry/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).not.toHaveBeenCalled();
  });
});
