/**
 * Regression tests: a partial/malformed /api/context/usage payload (fresh
 * session) used to crash the context panel ("Cannot read properties of
 * undefined (reading 'map')") and paint "NaN" in the Usage button.
 */
import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
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
    // Fresh sessions with zero offload receipts stay chrome-light.
    expect(screen.queryByText("Offloaded outputs")).not.toBeInTheDocument();
    expect(screen.queryByText("History compaction")).not.toBeInTheDocument();
    expect(screen.queryByText("Tool-output tokens avoided")).not.toBeInTheDocument();
    expect(screen.queryByText("Compact tool outputs saved")).not.toBeInTheDocument();
  });

  it("shows spill / history / tool-output honesty footer when counts are present", () => {
    const usageWithOffload: ContextUsageResponse = {
      total: 50000,
      limit: 100000,
      categories: [
        { name: "System prompt", tokens: 20000 },
        { name: "Conversation", tokens: 30000 },
      ],
      spill_count: 2,
      spill_chars: 3200,
      history_compactions: 1,
      history_tokens_saved: 1500,
      tool_output_tokens_saved: 900,
      tool_output_savings_usd: 0.006,
    };

    renderDock(usageWithOffload);

    expect(screen.getByText("Offloaded outputs")).toBeInTheDocument();
    expect(screen.getByText(/3\.2k chars \(2 spills\)/)).toBeInTheDocument();
    expect(screen.getByText("History compaction")).toBeInTheDocument();
    expect(screen.getByText(/1\.5k saved \(1 event\)/)).toBeInTheDocument();
    expect(screen.getByText("Tool-output tokens avoided")).toBeInTheDocument();
    expect(screen.getByText("900")).toBeInTheDocument();
    expect(screen.getByText("Compact tool outputs saved")).toBeInTheDocument();
    expect(screen.getByText("~$0.006")).toBeInTheDocument();
  });

  it("offers Compact now beside a pressured session warning", () => {
    const onCompact = vi.fn();
    window.addEventListener("harness-compact-session", onCompact);
    const pressuredUsage = {
      total: 125000,
      limit: 200000,
      categories: [{ name: "Conversation", tokens: 125000 }],
      compaction_advice: {
        level: "soon",
        needs_intervention: true,
        budget_kind: "absolute",
        budget_tokens: 120000,
      },
    } as ContextUsageResponse;

    try {
      renderDock(pressuredUsage);

      expect(screen.getByText("Long session")).toBeInTheDocument();
      expect(screen.getByText(/120k working-context budget/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Compact now" }));
      expect(onCompact).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener("harness-compact-session", onCompact);
    }
  });

  it("hides honesty footer lines when offload counts are zero or non-finite", () => {
    const usageZeroOffload: ContextUsageResponse = {
      total: 1000,
      limit: 100000,
      categories: [{ name: "Conversation", tokens: 1000 }],
      spill_count: 0,
      spill_chars: 0,
      history_compactions: 0,
      history_tokens_saved: 0,
      tool_output_tokens_saved: NaN as unknown as number,
      tool_output_savings_usd: -1,
    };

    renderDock(usageZeroOffload);

    expect(screen.queryByText("Offloaded outputs")).not.toBeInTheDocument();
    expect(screen.queryByText("History compaction")).not.toBeInTheDocument();
    expect(screen.queryByText("Tool-output tokens avoided")).not.toBeInTheDocument();
    expect(screen.queryByText("Compact tool outputs saved")).not.toBeInTheDocument();
  });
});
