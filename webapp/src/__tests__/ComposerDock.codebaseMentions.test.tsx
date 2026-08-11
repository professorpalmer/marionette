/**
 * @codebase mention picker: Scope row inserts via insertCodebase.
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
vi.mock("../components/conversation/WorkspaceChip", () => ({
  default: () => <div data-testid="workspace-chip" />,
}));

const noop = () => {};

describe("ComposerDock codebase mentions", () => {
  it("surfaces Codebase scope and inserts via insertCodebase", () => {
    const insertCodebase = vi.fn();
    render(
      <ComposerDock
        config={null}
        taRef={createRef<HTMLTextAreaElement>()}
        input="@code"
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
        mentionSearch="code"
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
        insertCodebase={insertCodebase}
        showCodebaseMention={true}
        insertSlashCommand={noop}
        handleQueueAdd={noop}
        stop={noop}
        send={noop}
      />,
    );

    expect(screen.getByText("Scope")).toBeInTheDocument();
    expect(screen.getByText("Codebase")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Codebase"));
    expect(insertCodebase).toHaveBeenCalledTimes(1);
  });

  it("shows No matches when the @mention picker has zero hits", () => {
    render(
      <ComposerDock
        config={null}
        taRef={createRef<HTMLTextAreaElement>()}
        input="@zzzz"
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
        mentionSearch="zzzz"
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
    expect(screen.getByTestId("mention-no-matches")).toHaveTextContent("No matches");
  });
});
