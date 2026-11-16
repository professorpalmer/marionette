import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import VoiceSettingsGroup from "../components/VoiceSettingsGroup";
import { readVoiceSettings, resetVoiceSettingsCache, VOICE_SETTINGS_KEY, writeVoiceSettings } from "../lib/voiceSettings";

const transport = vi.hoisted(() => ({ getJSON: vi.fn() }));
vi.mock("../lib/transport", () => ({ getJSON: transport.getJSON, postJSON: vi.fn() }));

type VoiceIpcStub = {
  status: ReturnType<typeof vi.fn>;
  openMicrophoneSettings: ReturnType<typeof vi.fn>;
};

function installIpc(status: { available: boolean; reason: string | null; microphone: string }): VoiceIpcStub {
  const voice = { status: vi.fn().mockResolvedValue(status), openMicrophoneSettings: vi.fn().mockResolvedValue(true) };
  (window as unknown as { harnessIPC: unknown }).harnessIPC = { voice };
  return voice;
}

beforeEach(() => {
  localStorage.removeItem(VOICE_SETTINGS_KEY);
  resetVoiceSettingsCache();
  writeVoiceSettings({ engine: "macos" });
  transport.getJSON.mockReset();
});
afterEach(() => {
  delete (window as unknown as { harnessIPC?: unknown }).harnessIPC;
});

describe("VoiceSettingsGroup", () => {
  it("shows the engine, language and send rows on the shared layout", async () => {
    installIpc({ available: true, reason: null, microphone: "granted" });
    render(<VoiceSettingsGroup />);
    const group = screen.getByTestId("voice-settings");
    expect(within(group).getByRole("heading", { name: "Voice" })).toBeInTheDocument();
    expect(screen.getByTestId("voice-row-engine")).toHaveTextContent("Dictation engine");
    expect(screen.getByTestId("voice-row-language")).toHaveTextContent("Language");
    expect(screen.getByRole("switch", { name: "Send when I say send" })).toHaveAttribute("aria-checked", "false");
    await waitFor(() => expect(screen.getByTestId("voice-row-engine")).toHaveTextContent("Ready"));
  });

  it("stores each choice in Voice settings", async () => {
    installIpc({ available: true, reason: null, microphone: "granted" });
    transport.getJSON.mockResolvedValue({ whisper: { available: true, reason: null, model: "ggml-small.en.bin" } });
    render(<VoiceSettingsGroup />);
    fireEvent.change(screen.getByRole("combobox", { name: "Dictation language" }), { target: { value: "en-GB" } });
    fireEvent.click(screen.getByRole("switch", { name: "Send when I say send" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Dictation engine" }), { target: { value: "whisper" } });
    expect(readVoiceSettings()).toEqual({ engine: "whisper", language: "en-GB", sendOnPhrase: true });
    expect(JSON.parse(localStorage.getItem(VOICE_SETTINGS_KEY) || "{}").engine).toBe("whisper");
    await waitFor(() => expect(screen.getByTestId("voice-row-engine")).toHaveTextContent("ggml-small.en.bin"));
    expect(transport.getJSON).toHaveBeenCalledWith("/api/voice/status");
  });

  it("says why the on-device engine is not available", async () => {
    installIpc({ available: false, reason: "On-device dictation needs macOS 26 or later.", microphone: "granted" });
    render(<VoiceSettingsGroup />);
    await waitFor(() => expect(screen.getByTestId("voice-row-engine")).toHaveTextContent("Not available"));
    expect(screen.getByTestId("voice-row-engine")).toHaveTextContent("needs macOS 26 or later");
  });

  it("offers the Privacy pane when the microphone is denied", async () => {
    const voice = installIpc({ available: true, reason: null, microphone: "denied" });
    render(<VoiceSettingsGroup />);
    const open = await screen.findByRole("button", { name: "Open Microphone settings" });
    expect(screen.getByTestId("voice-row-engine")).toHaveTextContent("Microphone access is off");
    fireEvent.click(open);
    expect(voice.openMicrophoneSettings).toHaveBeenCalledTimes(1);
  });

  it("names the missing whisper.cpp setup", async () => {
    writeVoiceSettings({ engine: "whisper" });
    transport.getJSON.mockResolvedValue({ whisper: { available: false, reason: "Install whisper.cpp (brew install whisper-cpp)." } });
    render(<VoiceSettingsGroup />);
    await waitFor(() => expect(screen.getByTestId("voice-row-engine")).toHaveTextContent("brew install whisper-cpp"));
  });
});
