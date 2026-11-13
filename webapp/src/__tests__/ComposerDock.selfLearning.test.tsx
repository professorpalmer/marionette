/**
 * The self-learning footer has a close button and links to Skills.
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
  distillNotice: string,
  onSetDistillNotice: (v: string | null) => void,
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
      distillNotice={distillNotice}
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
      onSetDistillNotice={onSetDistillNotice}
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
    />,
  );
}

describe("ComposerDock self-learning footer", () => {
  it("closes with a real button", () => {
    const onSet = vi.fn();
    renderDockWithNotice('Self-learning: proposed 1 skill ("Fix a test")', onSet);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onSet).toHaveBeenCalledWith(null);
  });

  it("opens Settings at Skills & Rules", () => {
    const onSet = vi.fn();
    const seen: string[] = [];
    const on = (e: Event) => seen.push(e.type + ((e as CustomEvent).detail ? `:${(e as CustomEvent).detail}` : ""));
    for (const t of ["harness-focus-tab", "harness-settings-page", "harness-expand-skills"]) window.addEventListener(t, on);
    renderDockWithNotice('Self-learning: proposed 1 skill ("Fix a test")', onSet);
    fireEvent.click(screen.getByRole("button", { name: "Review in Skills" }));
    expect(onSet).toHaveBeenCalledWith(null);
    expect(seen).toEqual(["harness-settings-page:advanced", "harness-focus-tab:settings", "harness-expand-skills"]);
    for (const t of ["harness-focus-tab", "harness-settings-page", "harness-expand-skills"]) window.removeEventListener(t, on);
  });

  it("has no Skills link on a wiki notice", () => {
    renderDockWithNotice("Wiki: filed 2 pages", vi.fn());
    expect(screen.queryByRole("button", { name: "Review in Skills" })).toBeNull();
  });
});
