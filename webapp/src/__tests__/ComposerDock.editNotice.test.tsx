/**
 * Regression: standalone editNotice banners must be dismissible.
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

function renderDockWithNotice(
  editNotice: string,
  onSetEditNotice: (v: string | null) => void,
) {
  return render(
    <ComposerDock
      config={null}
      taRef={createRef<HTMLTextAreaElement>()}
      input=""
      auto={false}
      plan={false}
      composerBusy={false}
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
      editNotice={editNotice}
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
      onSetEditNotice={onSetEditNotice}
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
    />,
  );
}

describe("ComposerDock editNotice dismiss", () => {
  it("dismisses a standalone editNotice with no edit/revert chrome", () => {
    const onSetEditNotice = vi.fn();

    renderDockWithNotice("Could not stop the current turn.", onSetEditNotice);

    expect(screen.getByText("Could not stop the current turn.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss notice" }));

    expect(onSetEditNotice).toHaveBeenCalledWith(null);
  });
});
