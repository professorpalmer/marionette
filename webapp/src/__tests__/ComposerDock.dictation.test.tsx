/**
 * Voice dictation in the composer: the mic button states, live text at the
 * cursor, no send unless "send" by voice is on, and the denied-microphone fix.
 */
import { useRef, useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ComposerDock from "../components/conversation/ComposerDock";
import { DictationError, type DictationCallbacks } from "../lib/dictationEngine";
import { resetVoiceSettingsCache, VOICE_SETTINGS_KEY, writeVoiceSettings } from "../lib/voiceSettings";

vi.mock("../lib/api", () => ({ api: {} }));
vi.mock("../components/PilotPicker", () => ({ default: () => <div data-testid="pilot-picker" /> }));
vi.mock("../components/SwarmReasoningPicker", () => ({ default: () => <div data-testid="swarm-reasoning-picker" /> }));
vi.mock("../components/conversation/WorkspaceChip", () => ({ default: () => <div data-testid="workspace-chip" /> }));

type Fake = {
  callbacks: DictationCallbacks;
  engine: string;
  language: string;
  finish: (text: string) => void;
  cancel: ReturnType<typeof vi.fn>;
};
const engine = vi.hoisted(() => ({
  sessions: [] as Fake[],
  failStart: null as null | { code: string; message: string },
}));
const openSettings = vi.hoisted(() => vi.fn());

vi.mock("../lib/dictationEngine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/dictationEngine")>();
  return {
    ...actual,
    openMicrophoneSettings: openSettings,
    startDictation: vi.fn(async (name: string, language: string, callbacks: DictationCallbacks) => {
      if (engine.failStart) throw new actual.DictationError(engine.failStart.code as never, engine.failStart.message);
      let resolveStop: (text: string) => void = () => {};
      const stopped = new Promise<string>((resolve) => { resolveStop = resolve; });
      const fake: Fake = { callbacks, engine: name, language, finish: (text) => resolveStop(text), cancel: vi.fn() };
      engine.sessions.push(fake);
      return { stop: () => stopped, cancel: fake.cancel };
    }),
  };
});

const noop = () => {};

function Harness({ initial = "", onSend }: { initial?: string; onSend: (text: string) => void }) {
  const [input, setInput] = useState(initial);
  const taRef = useRef<HTMLTextAreaElement>(null);
  return (
    <ComposerDock
      config={null}
      taRef={taRef}
      input={input}
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
      handleInputChange={(value) => setInput(value)}
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
      send={() => onSend(input)}
    />
  );
}

const box = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  localStorage.removeItem(VOICE_SETTINGS_KEY);
  resetVoiceSettingsCache();
  engine.sessions.length = 0;
  engine.failStart = null;
  openSettings.mockReset();
});

describe("ComposerDock dictation", () => {
  it("shows a mic button that starts, listens, and stops", async () => {
    render(<Harness onSend={noop} />);
    const mic = screen.getByRole("button", { name: "Start dictation" });
    expect(mic).toHaveAttribute("aria-pressed", "false");
    expect(mic).toHaveAttribute("data-state", "idle");

    fireEvent.click(mic);
    await flush();
    const listening = screen.getByRole("button", { name: "Stop dictation" });
    expect(listening).toHaveAttribute("aria-pressed", "true");
    expect(listening).toHaveTextContent("Listening");

    fireEvent.click(listening);
    expect(screen.getByRole("button", { name: "Start dictation" })).toHaveAttribute("data-state", "finishing");
    await act(async () => { engine.sessions[0].finish("done"); });
    expect(screen.getByRole("button", { name: "Start dictation" })).toHaveAttribute("data-state", "idle");
  });

  it("writes live partial text at the cursor and keeps the words around it", async () => {
    render(<Harness initial="Please check and report" onSend={noop} />);
    box().setSelectionRange(13, 13);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    act(() => engine.sessions[0].callbacks.onText("the build"));
    expect(box().value).toBe("Please check the build and report");
    act(() => engine.sessions[0].callbacks.onText("the build log"));
    expect(box().value).toBe("Please check the build log and report");
    expect(box().selectionStart).toBe("Please check the build log".length);
  });

  it("keeps a word the user types while dictation runs", async () => {
    render(<Harness onSend={noop} />);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    act(() => engine.sessions[0].callbacks.onText("open the"));
    expect(box().value).toBe("open the");
    fireEvent.input(box(), { target: { value: "open the settings" } });
    box().setSelectionRange(17, 17);
    act(() => engine.sessions[0].callbacks.onText("open the page"));
    expect(box().value).toBe("open the settings page");
  });

  it("does not send when dictation ends, even when the user says send", async () => {
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Stop dictation" }));
    await act(async () => { engine.sessions[0].finish("Fix the import, send."); });
    expect(box().value).toBe("Fix the import, send.");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("sends without the word send when send by voice is on", async () => {
    writeVoiceSettings({ sendOnPhrase: true });
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Stop dictation" }));
    await act(async () => { engine.sessions[0].finish("Fix the import, send."); });
    expect(box().value).toBe("Fix the import");
    expect(onSend).toHaveBeenCalledWith("Fix the import");
  });

  it("uses the engine and language from Voice settings", async () => {
    writeVoiceSettings({ engine: "whisper", language: "de-DE" });
    render(<Harness onSend={noop} />);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    expect(engine.sessions[0].engine).toBe("whisper");
    expect(engine.sessions[0].language).toBe("de-DE");
  });

  it("toggles with Cmd+Shift+D", async () => {
    render(<Harness onSend={noop} />);
    fireEvent.keyDown(window, { key: "D", metaKey: true, shiftKey: true });
    await flush();
    expect(screen.getByRole("button", { name: "Stop dictation" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "d", ctrlKey: true, shiftKey: true });
    await act(async () => { engine.sessions[0].finish(""); });
    expect(screen.getByRole("button", { name: "Start dictation" })).toHaveAttribute("data-state", "idle");
  });

  it("explains a denied microphone and opens the Privacy pane", async () => {
    engine.failStart = { code: "mic_denied", message: "Microphone access is off for Marionette." };
    render(<Harness onSend={noop} />);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("Microphone access is off for Marionette.");
    fireEvent.click(screen.getByRole("button", { name: "Open Microphone settings" }));
    expect(openSettings).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss dictation error" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows an engine failure after start and stops listening", async () => {
    render(<Harness onSend={noop} />);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    act(() => engine.sessions[0].callbacks.onError(new DictationError("engine_failed", "The dictation helper stopped.")));
    expect(screen.getByRole("alert")).toHaveTextContent("The dictation helper stopped.");
    expect(screen.queryByRole("button", { name: "Open Microphone settings" })).toBeNull();
    expect(screen.getByRole("button", { name: "Start dictation" })).toHaveAttribute("data-state", "error");
  });

  it("cancels a live session when the composer unmounts", async () => {
    const view = render(<Harness onSend={noop} />);
    fireEvent.click(screen.getByRole("button", { name: "Start dictation" }));
    await flush();
    view.unmount();
    expect(engine.sessions[0].cancel).toHaveBeenCalledTimes(1);
  });
});
