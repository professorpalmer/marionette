/**
 * Turn recovery: one failure gives one action (Continue or Retry), hidden
 * while busy, one dispatch per click.
 */
import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ComposerDock from "../components/conversation/ComposerDock";
import type { TurnRecovery } from "../lib/turnTerminal";

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
  recovery?: TurnRecovery | null;
  onRecover?: () => void;
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
      handleQueueClearAll={noop}
      handleQueueDragStart={noop}
      handleQueueDragOver={noop}
      handleQueueDragLeave={noop}
      handleQueueDrop={noop}
      moveServerQueueItem={noop}
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
      recovery={opts.recovery ?? null}
      onRecover={opts.onRecover}
    />,
  );
}

describe("ComposerDock recovery chrome", () => {
  it("hides the recovery action while the mouth is busy", () => {
    renderDock({ composerBusy: true, recovery: { action: "continue", cause: "provider_eof" } });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });

  it("shows only Continue for a turn that left work, and dispatches once", () => {
    const onRecover = vi.fn();
    renderDock({ composerBusy: false, recovery: { action: "continue", cause: "provider_eof" }, onRecover });
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(onRecover).toHaveBeenCalledTimes(1);
  });

  it("shows only Retry for a request that failed before any work", () => {
    const onRecover = vi.fn();
    renderDock({ composerBusy: false, recovery: { action: "retry", cause: "transport_error" }, onRecover });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRecover).toHaveBeenCalledTimes(1);
  });

  it("shows no recovery action when nothing failed", () => {
    renderDock({ composerBusy: false, recovery: null });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });
});
