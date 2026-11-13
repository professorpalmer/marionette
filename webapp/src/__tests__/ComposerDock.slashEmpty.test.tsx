/**
 * Regression: slash picker must show an empty-state when slashSearch is open
 * but slashMenu returns zero hits (parity with @mention no-matches).
 */
import { createRef } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetSlashMenuPrefsForTests } from "../components/conversation/slashMenuPrefs";
import ComposerDock from "../components/conversation/ComposerDock";
import { OPEN_ONBOARDING_EVENT } from "../lib/sessionNav";
import type { Config } from "../lib/api";

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

function renderDock(slashSearch: string | null, config: Config | null = null) {
  return render(
    <ComposerDock
      config={config}
      taRef={createRef<HTMLTextAreaElement>()}
      input={slashSearch !== null ? `/${slashSearch}` : ""}
      auto={false}
      plan={false}
      composerBusy={false}
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
      slashSearch={slashSearch}
      selectedSlashIndex={0}
      allSlashCommands={[
        { cmd: "/help", desc: "Show help" },
        { cmd: "/clear", desc: "Clear transcript" },
        { cmd: "/refine", desc: "Propose a refine", visibility: "expert" },
      ]}
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
    />,
  );
}

describe("ComposerDock slash empty-state", () => {
  afterEach(() => resetSlashMenuPrefsForTests());

  it("shows No matches when slashSearch is open with zero hits", () => {
    renderDock("zzzz-no-such-command");
    expect(screen.getByTestId("slash-no-matches")).toHaveTextContent("No matches");
  });

  it("lists matching commands when hits exist", () => {
    renderDock("help");
    expect(screen.queryByTestId("slash-no-matches")).toBeNull();
    expect(screen.getByText("Show help")).toBeInTheDocument();
    expect(screen.getByRole("listbox", { name: "Commands" })).toBeInTheDocument();
  });

  it("hides the picker when slashSearch is null", () => {
    renderDock(null);
    expect(screen.queryByTestId("slash-no-matches")).toBeNull();
    expect(screen.queryByRole("listbox", { name: "Commands" })).toBeNull();
    expect(screen.queryByText("Show help")).toBeNull();
  });

  it("hides internal commands until expert mode is on", () => {
    renderDock("");
    expect(screen.queryByText("/refine")).toBeNull();
    const expert = screen.getByRole("button", { name: "Expert" });
    expect(expert).toHaveAttribute("aria-pressed", "false");
    expect(expert.getAttribute("title")).toContain("show 1 internal command");
    act(() => { fireEvent.click(expert); });
    expect(screen.getByRole("button", { name: "Expert" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("/refine")).toBeInTheDocument();
  });
});

describe("ComposerDock with no model", () => {
  const noKey = { driver: "qwen3-coder-30b", reach: "openrouter", budget: 0, pilot_ready: false } as Config;

  it("shows Connect a model in place of a fallback model name", () => {
    const onboarding = vi.fn();
    window.addEventListener(OPEN_ONBOARDING_EVENT, onboarding);
    renderDock(null, noKey);
    expect(screen.queryByTestId("pilot-picker")).toBeNull();
    expect(screen.queryByTestId("swarm-reasoning-picker")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Connect a model" }));
    expect(onboarding).toHaveBeenCalledTimes(1);
    window.removeEventListener(OPEN_ONBOARDING_EVENT, onboarding);
  });

  it("keeps the picker for a ready pilot and for a keyless local pilot", () => {
    renderDock(null, { ...noKey, pilot_ready: true });
    expect(screen.getByTestId("pilot-picker")).toBeInTheDocument();
    cleanup();
    renderDock(null, { ...noKey, driver: "local:qwen3-coder-30b" });
    expect(screen.getByTestId("pilot-picker")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect a model" })).toBeNull();
  });
});
