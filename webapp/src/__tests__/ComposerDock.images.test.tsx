import { createRef } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
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

function renderImageDock(previewUrl: string, onLightbox: (url: string | null) => void) {
  return render(
    <ComposerDock
      config={null}
      taRef={createRef<HTMLTextAreaElement>()}
      input=""
      auto={false}
      plan={false}
      composerBusy={true}
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
      attachedImages={[{path:"input:session:sha", name:"original.png", previewUrl}]}
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
      onSetLightboxUrl={onLightbox}
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


afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); Reflect.deleteProperty(window, "__HARNESS_TOKEN__"); });
it("renders copied saved images with the real authenticated transport and passes a durable lightbox locator", async () => {
  Reflect.set(window, "__HARNESS_TOKEN__", "owner-test");
  vi.stubGlobal("fetch", vi.fn(async (path, init) => {
    if (path === "/api/endpoint") return Response.json({ok:true, protocol_version:1, endpoint_id:"e", boot_id:"b", capabilities:["endpoint_fence_v1"]});
    expect(path).toBe("/api/image?path=input%3Asession%3Asha");
    expect(init.headers["X-Harness-Token"]).toBe("owner-test");
    return new Response("pixels", {headers:{"Content-Type":"image/png"}});
  }));
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:composer-authenticated");
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.stubGlobal("Image", class { onload: (() => void) | null = null; onerror = null; set src(value: string) { if(value) queueMicrotask(() => this.onload?.()); } });
  const open = vi.fn();
  const view = renderImageDock("/api/image?path=input%3Asession%3Asha", open);
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByAltText("original.png")).toHaveAttribute("src", "blob:composer-authenticated");
  fireEvent.click(screen.getByAltText("original.png"));
  expect(open).toHaveBeenCalledWith("/api/image?path=input%3Asession%3Asha");
  view.unmount();
  expect(revoke).toHaveBeenCalledWith("blob:composer-authenticated");
});

it("preserves composer ownership of local blob previews", () => {
  vi.stubGlobal("fetch", vi.fn());
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  const view = renderImageDock("blob:local-preview", vi.fn());
  expect(screen.getByAltText("original.png")).toHaveAttribute("src", "blob:local-preview");
  expect(fetch).not.toHaveBeenCalled();
  view.unmount();
  expect(revoke).not.toHaveBeenCalled();
});
