/**
 * Empty busy composer must not default to Steer. Stop is the only send-row
 * action until the operator types a redirect.
 */
import { createRef, type ComponentProps } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import css from "../index.css?raw";
import postcss from "postcss";
import ComposerDock from "../components/conversation/ComposerDock";

vi.mock("../lib/api", () => ({
  api: {},
}));
vi.mock("../components/conversation/WorkspaceChip", () => ({
  default: () => <div data-testid="workspace-chip" />,
}));

const noop = () => {};

function renderBusyDock(input: string, overrides: Partial<ComponentProps<typeof ComposerDock>> = {}) {
  return render(
    <ComposerDock
      config={{ driver: "codex:gpt-6-astra", models: ["codex:gpt-6-astra"], model_labels: { "codex:gpt-6-astra": "GPT-6 Astra Long Model Name" }, reach: "local", budget: 1, reasoning_effort: "high" }}
      taRef={createRef<HTMLTextAreaElement>()}
      input={input}
      auto={false}
      plan={false}
      composerBusy={true}
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
      {...overrides}
    />,
  );
}

describe("ComposerDock busy chrome", () => {
  it("shows only Stop when the busy composer is empty", () => {
    renderBusyDock("");
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /steer/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /interrupt/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /queue/i })).toBeNull();
  });

  it("reveals Steer, Interrupt, and Queue once a redirect is typed", () => {
    renderBusyDock("pivot to auth");
    expect(screen.getByRole("button", { name: /steer/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /interrupt/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /queue/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });

  it("keeps the composer as a solid panel island", () => {
    const { container } = renderBusyDock("");
    const dock = container.querySelector(".composer-dock");
    expect(dock).toHaveClass("bg-panel2/80");
    expect(dock?.className).not.toMatch(/shell-inset-glass/);
  });

  it("keeps send actions in a non-shrinking cluster so the picker can truncate", () => {
    const { container } = renderBusyDock("");
    expect(container.querySelector(".composer-toolbar")).toBeTruthy();
    expect(container.querySelector(".composer-toolbar-actions")).toBeTruthy();
    expect(container.querySelector(".composer-toolbar-send")).toBeTruthy();
    expect(container.querySelector(".pilot-picker-slot")).toBeTruthy();
  });
});

describe("ComposerDock picker layout and accessibility", () => {
  it.each([false, true])("exposes icon toggles with stable names and pressed state %s", (pressed) => {
    renderBusyDock("", { auto: pressed, plan: pressed });
    for (const name of ["Autopilot", "Plan mode"]) {
      const button = screen.getByRole("button", { name, exact: true });
      expect(button).toHaveAttribute("aria-pressed", String(pressed));
      expect(button).toHaveAttribute("title");
      expect(button.textContent).toBe("");
      expect(button.querySelector("svg")).toBeTruthy();
    }
  });

  it("mounts the real model, pilot effort and separate Workers pickers", () => {
    renderBusyDock("");
    fireEvent.click(screen.getByRole("button", { name: /GPT-6 Astra Long Model Name/ }));
    expect(screen.getByRole("dialog", { name: "Pilot model picker" })).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    fireEvent.click(screen.getByTitle("Reasoning effort (High)"));
    expect(screen.getByRole("dialog", { name: "Reasoning effort picker" })).toBeInTheDocument();
    expect(screen.getByTestId("swarm-reasoning-picker")).toHaveTextContent("Workers");
  });

  it("allocates uncapped picker space and wraps within narrow containers", () => {
    const root = postcss.parse(css);
    const declarations = (selector: string) => {
      const result: Record<string, string> = {};
      root.walkRules(selector, rule => { rule.walkDecls(decl => { result[decl.prop] = decl.value; }); });
      return result;
    };
    expect(declarations(".composer-dock")["container-type"]).toBe("inline-size");
    expect(declarations(".pilot-picker-slot")["max-width"]).toBeUndefined();
    expect(declarations(".pilot-picker-slot").flex).toBe("1 1 20rem");
    expect(declarations(".composer-toolbar-actions")["flex-wrap"]).toBe("wrap");
    expect(declarations(".pilot-picker-controls")["flex-wrap"]).toBe("wrap");
    expect(declarations(".pilot-model-slot").flex).toBe("1 1 12rem");
    expect(css).not.toMatch(/\.composer-toolbar-label\s*\{\s*display:\s*none/);
    const { container } = renderBusyDock("");
    expect(container.querySelector(".pilot-picker-slot .pilot-picker-controls .pilot-model-slot button")).toHaveTextContent("GPT-6 Astra Long Model Name");
  });
});

describe("ComposerDock action preservation", () => {
  it.each([
    { auto: false, plan: false, label: "Send" },
    { auto: true, plan: false, label: "Run" },
    { auto: false, plan: true, label: "Plan" },
  ])("retains the idle $label branch and keyboard handler", ({ auto, plan, label }) => {
    const send = vi.fn();
    const handleKeyDown = vi.fn();
    renderBusyDock("do the work", { composerBusy: false, auto, plan, send, handleKeyDown });
    fireEvent.click(screen.getByRole("button", { name: label, exact: true }));
    expect(send).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", shiftKey: true });
    expect(handleKeyDown).toHaveBeenCalledTimes(1);
  });

  it("keeps mode toggles mutually exclusive", () => {
    const onSetAuto = vi.fn();
    const onSetPlan = vi.fn();
    renderBusyDock("", { onSetAuto, onSetPlan });
    fireEvent.click(screen.getByRole("button", { name: "Autopilot", exact: true }));
    const autoUpdater = onSetAuto.mock.calls[0][0];
    expect(autoUpdater(false)).toBe(true);
    expect(onSetPlan).toHaveBeenCalledWith(false);
    fireEvent.click(screen.getByRole("button", { name: "Plan mode", exact: true }));
    const planUpdater = onSetPlan.mock.calls.at(-1)?.[0];
    expect(planUpdater(false)).toBe(true);
    expect(onSetAuto).toHaveBeenCalledWith(false);
  });
});
