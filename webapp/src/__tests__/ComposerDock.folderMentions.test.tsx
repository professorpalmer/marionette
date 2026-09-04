/**
 * @folder mention picker: folders render with a folder label and insert
 * via the dedicated insertFolder callback (honest @folder: token).
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

describe("ComposerDock folder mentions", () => {
  it("surfaces folder hits and inserts via insertFolder", () => {
    const insertFolder = vi.fn();
    render(
      <ComposerDock
        config={null}
        taRef={createRef<HTMLTextAreaElement>()}
        input="@src"
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
        showContextPanel={false}
        contextUsage={null}
        mentionSearch="src"
        filteredFiles={["src/a.ts"]}
        filteredFolders={["src", "src/lib"]}
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
        insertFolder={insertFolder}
        insertSymbol={noop}
        insertCodebase={noop}
        showCodebaseMention={false}
        insertSlashCommand={noop}
        handleQueueAdd={noop}
        stop={noop}
        send={noop}
      />,
    );

    expect(screen.getByText("Folders")).toBeInTheDocument();
    expect(screen.getAllByText("folder").length).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByText("src/lib"));
    expect(insertFolder).toHaveBeenCalledWith("src/lib");
  });
});
