import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DictationError, pcmLevel, startDictation, type DictationCallbacks } from "../lib/dictationEngine";

const transport = vi.hoisted(() => ({ getJSON: vi.fn(), postJSON: vi.fn() }));
vi.mock("../lib/transport", () => transport);

class FakeTrack extends EventTarget {
  stopped = false;
  stop() { this.stopped = true; }
}

const audio = { processor: null as null | { onaudioprocess: ((event: unknown) => void) | null }, track: new FakeTrack() };

class FakeAudioContext {
  sampleRate = 16_000;
  destination = {};
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createScriptProcessor() {
    const node = { onaudioprocess: null as ((event: unknown) => void) | null, connect() {}, disconnect() {} };
    audio.processor = node;
    return node;
  }
  close() { return Promise.resolve(); }
}

function speak(value: number) {
  const samples = new Float32Array(2048).fill(value);
  audio.processor?.onaudioprocess?.({ inputBuffer: { getChannelData: () => samples } });
}

function callbacks() {
  return { onText: vi.fn(), onLevel: vi.fn(), onError: vi.fn() } satisfies DictationCallbacks;
}

function installIpc() {
  let release: (value: { ok: true; id: string }) => void = () => {};
  let listener: (event: unknown) => void = () => {};
  const voice = {
    status: vi.fn().mockResolvedValue({ available: true, reason: null, microphone: "granted" }),
    requestMicrophone: vi.fn().mockResolvedValue({ granted: true, status: "granted" }),
    openMicrophoneSettings: vi.fn(),
    start: vi.fn(() => new Promise((resolve) => { release = resolve; })),
    write: vi.fn(),
    stop: vi.fn(),
    cancel: vi.fn(),
    onEvent: vi.fn((cb: (event: unknown) => void) => { listener = cb; return () => {}; }),
  };
  (window as unknown as { harnessIPC: unknown }).harnessIPC = { voice };
  return { voice, release: (id: string) => release({ ok: true, id }), emit: (event: unknown) => listener(event) };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  audio.processor = null;
  audio.track = new FakeTrack();
  vi.stubGlobal("AudioContext", FakeAudioContext);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [audio.track], getAudioTracks: () => [audio.track] })) },
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as { harnessIPC?: unknown }).harnessIPC;
});

describe("pcmLevel", () => {
  it("is 0 for silence and 1 for loud speech", () => {
    expect(pcmLevel(new Int16Array(10))).toBe(0);
    expect(pcmLevel(new Int16Array(10).fill(16_000))).toBe(1);
    expect(pcmLevel(new Int16Array(0))).toBe(0);
  });
});

describe("on-device engine", () => {
  it("keeps audio from before the helper starts and sends it first", async () => {
    const ipc = installIpc();
    const cb = callbacks();
    const pending = startDictation("macos", "en-US", cb);
    await tick();
    speak(0.5);
    speak(0.25);
    expect(ipc.voice.write).not.toHaveBeenCalled();
    expect(cb.onLevel).toHaveBeenCalledTimes(2);
    ipc.release("voice-1");
    const session = await pending;
    expect(ipc.voice.write).toHaveBeenCalledTimes(2);
    expect(ipc.voice.write.mock.calls.map(([id]) => id)).toEqual(["voice-1", "voice-1"]);
    expect(new Int16Array(ipc.voice.write.mock.calls[0][1])[0]).toBe(16383);
    speak(0.1);
    expect(ipc.voice.write).toHaveBeenCalledTimes(3);

    ipc.emit({ id: "voice-1", type: "partial", text: "hello" });
    ipc.emit({ id: "other", type: "partial", text: "not mine" });
    expect(cb.onText).toHaveBeenCalledWith("hello");
    expect(cb.onText).toHaveBeenCalledTimes(1);
    const stopped = session.stop();
    expect(ipc.voice.stop).toHaveBeenCalledWith("voice-1");
    expect(audio.track.stopped).toBe(true);
    ipc.emit({ id: "voice-1", type: "done", text: "Hello." });
    await expect(stopped).resolves.toBe("Hello.");
  });

  it("reports a lost microphone and cancels the helper", async () => {
    const ipc = installIpc();
    const cb = callbacks();
    const pending = startDictation("macos", "en-US", cb);
    await tick();
    ipc.release("voice-2");
    await pending;
    audio.track.dispatchEvent(new Event("ended"));
    expect(cb.onError).toHaveBeenCalledTimes(1);
    const error = cb.onError.mock.calls[0][0] as DictationError;
    expect(error.code).toBe("mic_lost");
    expect(ipc.voice.cancel).toHaveBeenCalledWith("voice-2");
  });

  it("stops the microphone when the helper does not start", async () => {
    const ipc = installIpc();
    ipc.voice.start.mockResolvedValueOnce({ ok: false, code: "helper_missing", error: "no helper" } as never);
    await expect(startDictation("macos", "en-US", callbacks())).rejects.toMatchObject({ code: "engine_unavailable", message: "no helper" });
    expect(audio.track.stopped).toBe(true);
  });

  it("refuses before the microphone opens when access is off", async () => {
    const ipc = installIpc();
    ipc.voice.requestMicrophone.mockResolvedValueOnce({ granted: false, status: "denied" });
    await expect(startDictation("macos", "en-US", callbacks())).rejects.toMatchObject({ code: "mic_denied" });
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });
});

describe("whisper engine", () => {
  it("names the missing setup", async () => {
    transport.getJSON.mockResolvedValueOnce({ whisper: { available: false, reason: "Install whisper.cpp." } });
    await expect(startDictation("whisper", "en-US", callbacks())).rejects.toMatchObject({ code: "engine_unavailable", message: "Install whisper.cpp." });
  });

  it("posts the recorded audio on stop and reports a lost microphone", async () => {
    transport.getJSON.mockResolvedValueOnce({ whisper: { available: true } });
    transport.postJSON.mockResolvedValueOnce({ text: "final words" });
    const cb = callbacks();
    const session = await startDictation("whisper", "de-DE", cb);
    speak(0.5);
    await expect(session.stop()).resolves.toBe("final words");
    expect(transport.postJSON).toHaveBeenCalledWith("/api/voice/transcribe", expect.objectContaining({ language: "de-DE" }));

    transport.getJSON.mockResolvedValueOnce({ whisper: { available: true } });
    const lost = callbacks();
    await startDictation("whisper", "en-US", lost);
    audio.track.dispatchEvent(new Event("ended"));
    expect((lost.onError.mock.calls[0][0] as DictationError).code).toBe("mic_lost");
  });
});
