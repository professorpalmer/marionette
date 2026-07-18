/**
 * Regression tests: a partial/malformed /api/context/usage payload (fresh
 * session) used to crash the context panel ("Cannot read properties of
 * undefined (reading 'map')") and paint "NaN" in the Usage button.
 */
import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ComposerDock from "../components/conversation/ComposerDock";
import type { ContextUsageResponse } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {},
}));
vi.mock("../components/PilotPicker", () => ({
  default: () => <div data-testid="pilot-picker" />,
}));
vi.mock("../components/conversation/WorkspaceChip", () => ({
  default: () => <div data-testid="workspace-chip" />,
}));

const noop = () => {};

function renderDock(contextUsage: ContextUsageResponse | null) {
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
      editNotice={null}
      editBusy={false}
      showContextPanel={true}
      contextUsage={contextUsage}
      mentionSearch={null}
      filteredFiles={[]}
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
      insertSymbol={noop}
      insertSlashCommand={noop}
      handleQueueAdd={noop}
      stop={noop}
      send={noop}
    />,
  );
}

describe("ComposerDock context-usage resilience", () => {
  it("renders the open panel without crashing when categories is missing", () => {
    const partialUsage = {
      total: 1200,
      limit: 200000,
    } as ContextUsageResponse;

    const { container } = renderDock(partialUsage);

    expect(screen.getByText("Context Usage")).toBeInTheDocument();
    expect(screen.getByText("1% Full")).toBeInTheDocument();
    expect(container.textContent).not.toContain("NaN");
  });

  it("shows 0% and no NaN text when total and limit are non-finite", () => {
    const nanUsage = {
      total: NaN,
      limit: NaN,
      categories: undefined,
    } as unknown as ContextUsageResponse;

    const { container } = renderDock(nanUsage);

    expect(screen.getByText("0% Full")).toBeInTheDocument();
    expect(screen.getByText("0%")).toBeInTheDocument(); // Usage button
    expect(container.textContent).not.toContain("NaN");
  });

  it("still renders real values and category rows for a valid payload", () => {
    const validUsage: ContextUsageResponse = {
      total: 50000,
      limit: 100000,
      categories: [
        { name: "System prompt", tokens: 20000 },
        { name: "Conversation", tokens: 30000 },
      ],
    };

    const { container } = renderDock(validUsage);

    expect(screen.getByText("50% Full")).toBeInTheDocument();
    expect(screen.getByText("System prompt")).toBeInTheDocument();
    expect(screen.getByText("Conversation")).toBeInTheDocument();
    expect(container.textContent).not.toContain("NaN");
  });
});
