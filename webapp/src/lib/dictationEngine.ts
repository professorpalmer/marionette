import { getJSON, postJSON } from "./transport";
import type { VoiceEngine } from "./voiceSettings";

/**
 * Voice dictation engines. Both record the microphone in this window as 16 kHz
 * mono PCM. "macos" streams it to the on-device helper in the Electron shell;
 * "whisper" posts it to the backend, which runs whisper.cpp.
 */

export const SAMPLE_RATE = 16_000;
/** The backend refuses more than this (harness/api/voice.py MAX_SECONDS). */
export const WHISPER_MAX_SECONDS = 180;
const WHISPER_PARTIAL_MS = 1_200;
const STOP_TIMEOUT_MS = 8_000;

export type DictationErrorCode = "mic_denied" | "mic_unavailable" | "mic_lost" | "engine_unavailable" | "engine_failed";

export class DictationError extends Error {
  readonly code: DictationErrorCode;
  constructor(code: DictationErrorCode, message: string) {
    super(message);
    this.code = code;
  }
}

export type DictationCallbacks = {
  /** The whole spoken text so far. It can change words already shown. */
  onText: (text: string) => void;
  /** Microphone loudness, 0 to 1, about eight times a second. */
  onLevel: (level: number) => void;
  /** A failure after start. The session is over. */
  onError: (error: DictationError) => void;
};

export type DictationSession = {
  /** Stop listening and return the final text. */
  stop: () => Promise<string>;
  /** Stop listening and drop the text. */
  cancel: () => void;
};

type HelperEvent =
  | { id: string; type: "ready" }
  | { id: string; type: "partial" | "final" | "done"; text: string }
  | { id: string; type: "error"; code: string; message: string }
  | { id: string; type: "exit"; code: number | null; stderr?: string };

type VoiceIpc = {
  status: () => Promise<{ available: boolean; reason: string | null; microphone: string }>;
  requestMicrophone: () => Promise<{ granted: boolean; status: string }>;
  openMicrophoneSettings: () => Promise<boolean>;
  start: (payload: { locale: string }) => Promise<{ ok: true; id: string } | { ok: false; code: string; error: string }>;
  write: (id: string, chunk: ArrayBuffer) => void;
  stop: (id: string) => void;
  cancel: (id: string) => void;
  onEvent: (cb: (event: unknown) => void) => () => void;
};

export function voiceIpc(): VoiceIpc | null {
  if (typeof window === "undefined") return null;
  return (window as { harnessIPC?: { voice?: VoiceIpc } }).harnessIPC?.voice ?? null;
}

export const MICROPHONE_LOST_MESSAGE = "The microphone was disconnected. Dictation stopped.";

export const MICROPHONE_DENIED_MESSAGE =
  "Microphone access is off for Marionette. Turn it on in System Settings > Privacy & Security > Microphone.";

function parseHelperEvent(raw: unknown): HelperEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const id = typeof record.id === "string" ? record.id : null;
  if (!id) return null;
  switch (record.type) {
    case "ready":
      return { id, type: "ready" };
    case "partial":
    case "final":
    case "done":
      return { id, type: record.type, text: typeof record.text === "string" ? record.text : "" };
    case "error":
      return { id, type: "error", code: String(record.code ?? "speech_failed"), message: String(record.message ?? "Dictation failed.") };
    case "exit":
      return {
        id, type: "exit",
        code: typeof record.code === "number" ? record.code : null,
        stderr: typeof record.stderr === "string" ? record.stderr : undefined,
      };
    default:
      return null;
  }
}

/** Float samples at `fromRate` to 16 kHz signed 16-bit samples. */
export function toPcm16(samples: Float32Array, fromRate: number): Int16Array {
  const ratio = fromRate / SAMPLE_RATE;
  const length = Math.floor(samples.length / ratio);
  const out = new Int16Array(length);
  for (let i = 0; i < length; i++) {
    const value = samples[Math.min(samples.length - 1, Math.floor(i * ratio))];
    const clamped = Math.max(-1, Math.min(1, value));
    out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
  }
  return out;
}

/** Loudness of one chunk, 0 to 1. Speech at a normal distance is about 0.3 to 0.8. */
export function pcmLevel(chunk: Int16Array): number {
  if (chunk.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
  const rms = Math.sqrt(sum / chunk.length) / 0x8000;
  return Math.min(1, rms * 8);
}

export function pcm16ToBase64(chunks: readonly Int16Array[]): string {
  let binary = "";
  for (const chunk of chunks) {
    const bytes = new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength);
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
  }
  return btoa(binary);
}

type MicCapture = { stop: () => void };

type MicHandlers = {
  onPcm: (chunk: Int16Array) => void;
  /** The device went away (unplugged, or the OS took it back). */
  onLost: () => void;
};

/** Record the default microphone and hand 16 kHz PCM chunks to `onPcm`. */
export async function captureMicrophone({ onPcm, onLost }: MicHandlers): Promise<MicCapture> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new DictationError("mic_unavailable", "This window cannot open a microphone.");
  }
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    const name = error instanceof DOMException ? error.name : "";
    if (name === "NotAllowedError" || name === "SecurityError") throw new DictationError("mic_denied", MICROPHONE_DENIED_MESSAGE);
    if (name === "NotFoundError" || name === "OverconstrainedError") throw new DictationError("mic_unavailable", "No microphone was found.");
    throw new DictationError("mic_unavailable", `The microphone did not open: ${String(error)}`);
  }
  const context = new AudioContext({ sampleRate: SAMPLE_RATE });
  const source = context.createMediaStreamSource(stream);
  // ScriptProcessorNode, not an AudioWorklet: a worklet module cannot load from
  // the file:// origin of the packaged app. 2048 frames is 128 ms at 16 kHz.
  const processor = context.createScriptProcessor(2048, 1, 1);
  processor.onaudioprocess = (event) => {
    onPcm(toPcm16(event.inputBuffer.getChannelData(0), context.sampleRate));
  };
  source.connect(processor);
  processor.connect(context.destination);
  let stopped = false;
  const lost = () => { if (!stopped) onLost(); };
  for (const track of stream.getAudioTracks()) track.addEventListener("ended", lost);
  return {
    stop: () => {
      if (stopped) return;
      stopped = true;
      processor.onaudioprocess = null;
      source.disconnect();
      processor.disconnect();
      for (const track of stream.getTracks()) track.stop();
      void context.close();
    },
  };
}

async function startMacos(language: string, callbacks: DictationCallbacks): Promise<DictationSession> {
  const ipc = voiceIpc();
  if (!ipc) throw new DictationError("engine_unavailable", "On-device dictation needs the Marionette desktop app.");
  const status = await ipc.status();
  if (!status.available) throw new DictationError("engine_unavailable", status.reason || "On-device dictation is not available.");
  const access = await ipc.requestMicrophone();
  if (!access.granted) throw new DictationError("mic_denied", MICROPHONE_DENIED_MESSAGE);

  // The microphone opens first. Audio waits here until the helper runs, so
  // the first words are not lost while it starts.
  let id: string | null = null;
  const waiting: ArrayBuffer[] = [];
  let lostEarly = false;
  let onLost = () => { lostEarly = true; };
  const mic = await captureMicrophone({
    onPcm: (chunk) => {
      callbacks.onLevel(pcmLevel(chunk));
      const bytes = chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength) as ArrayBuffer;
      if (id === null) waiting.push(bytes);
      else ipc.write(id, bytes);
    },
    onLost: () => onLost(),
  });
  let started: Awaited<ReturnType<VoiceIpc["start"]>>;
  try {
    started = await ipc.start({ locale: language });
  } catch (error) {
    mic.stop();
    throw new DictationError("engine_unavailable", String(error));
  }
  if (!started.ok) {
    mic.stop();
    throw new DictationError("engine_unavailable", started.error);
  }
  const sessionId = started.id;

  let text = "";
  let finished = false;
  let resolveDone: (text: string) => void = () => {};
  const done = new Promise<string>((resolve) => { resolveDone = resolve; });
  const finish = (final: string) => {
    if (finished) return;
    finished = true;
    unsubscribe();
    resolveDone(final);
  };
  const fail = (error: DictationError) => {
    mic.stop();
    ipc.cancel(sessionId);
    finish(text);
    callbacks.onError(error);
  };
  const unsubscribe = ipc.onEvent((raw) => {
    const event = parseHelperEvent(raw);
    if (!event || event.id !== sessionId) return;
    switch (event.type) {
      case "ready":
        return;
      case "partial":
      case "final":
        text = event.text;
        callbacks.onText(text);
        return;
      case "done":
        text = event.text;
        finish(text);
        return;
      case "error":
        fail(new DictationError("engine_failed", event.message));
        return;
      case "exit":
        if (!finished && event.code !== 0) {
          fail(new DictationError("engine_failed", event.stderr || "The dictation helper stopped."));
        }
        finish(text);
        return;
      default: {
        const _exhaustive: never = event;
        return _exhaustive;
      }
    }
  });

  id = sessionId;
  for (const bytes of waiting.splice(0)) ipc.write(sessionId, bytes);
  onLost = () => fail(new DictationError("mic_lost", MICROPHONE_LOST_MESSAGE));
  if (lostEarly) onLost();

  return {
    stop: () => {
      mic.stop();
      ipc.stop(sessionId);
      const timeout = new Promise<string>((resolve) => setTimeout(() => resolve(text), STOP_TIMEOUT_MS));
      return Promise.race([done, timeout]).then((final) => {
        finish(final);
        return final;
      });
    },
    cancel: () => {
      mic.stop();
      ipc.cancel(sessionId);
      finish("");
    },
  };
}

type WhisperStatus = { whisper?: { available?: boolean; reason?: string | null } };
type WhisperReply = { text?: string; error?: string };

function whisperFailure(error: unknown): DictationError {
  const record = error && typeof error === "object" ? error as Record<string, unknown> : {};
  const message = typeof record.error === "string" ? record.error : error instanceof Error ? error.message : "Whisper did not answer.";
  return new DictationError("engine_failed", message);
}

async function startWhisper(language: string, callbacks: DictationCallbacks): Promise<DictationSession> {
  const status = await getJSON<WhisperStatus>("/api/voice/status");
  if (!status.whisper?.available) {
    throw new DictationError("engine_unavailable", status.whisper?.reason || "The local Whisper engine is not set up.");
  }
  const chunks: Int16Array[] = [];
  let samples = 0;
  let sentSamples = 0;
  let inflight: Promise<void> | null = null;
  let stopped = false;
  let failed = false;

  const transcribe = async (): Promise<string> => {
    sentSamples = samples;
    const reply = await postJSON<WhisperReply>("/api/voice/transcribe", { pcm16: pcm16ToBase64(chunks), language });
    return reply.text ?? "";
  };
  const partial = () => {
    if (inflight || stopped || failed || samples - sentSamples < SAMPLE_RATE / 2) return;
    inflight = transcribe()
      .then((text) => { if (!stopped) callbacks.onText(text); })
      .catch((error) => {
        failed = true;
        mic.stop();
        clearInterval(timer);
        callbacks.onError(whisperFailure(error));
      })
      .finally(() => { inflight = null; });
  };

  const mic = await captureMicrophone({
    onPcm: (chunk) => {
      callbacks.onLevel(pcmLevel(chunk));
      if (samples >= SAMPLE_RATE * WHISPER_MAX_SECONDS) return;
      chunks.push(chunk);
      samples += chunk.length;
    },
    onLost: () => {
      if (stopped || failed) return;
      failed = true;
      stopped = true;
      clearInterval(timer);
      mic.stop();
      callbacks.onError(new DictationError("mic_lost", MICROPHONE_LOST_MESSAGE));
    },
  });
  const timer = setInterval(partial, WHISPER_PARTIAL_MS);

  return {
    stop: async () => {
      stopped = true;
      clearInterval(timer);
      mic.stop();
      if (inflight) await inflight;
      if (failed || samples === 0) return "";
      try {
        return await transcribe();
      } catch (error) {
        throw whisperFailure(error);
      }
    },
    cancel: () => {
      stopped = true;
      clearInterval(timer);
      mic.stop();
    },
  };
}

export function startDictation(engine: VoiceEngine, language: string, callbacks: DictationCallbacks): Promise<DictationSession> {
  switch (engine) {
    case "macos":
      return startMacos(language, callbacks);
    case "whisper":
      return startWhisper(language, callbacks);
    default: {
      const _exhaustive: never = engine;
      return _exhaustive;
    }
  }
}

export function openMicrophoneSettings(): void {
  void voiceIpc()?.openMicrophoneSettings();
}
