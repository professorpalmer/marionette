"use strict";

// On-device voice dictation for macOS 26 and later. The renderer records the
// microphone and sends 16 kHz PCM chunks here; the speech-macos helper turns
// them into text with SpeechAnalyzer and this bridge forwards its JSON lines.

const fs = require("node:fs");
const path = require("node:path");
const { spawn: nodeSpawn } = require("node:child_process");
const {
  compileDevelopmentMacHelper,
  packagedSpeechHelperPath,
  speechSourcePath,
} = require("./build-native-computer.cjs");

const MICROPHONE_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";
const MAX_CHUNK_BYTES = 64 * 1024;
const LOCALE_PATTERN = /^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$/;

function macMajor(release) {
  // Darwin 25 is macOS 26.
  const darwin = Number.parseInt(String(release || "").split(".")[0], 10);
  return Number.isFinite(darwin) ? darwin + 1 : 0;
}

function createSpeechBridge({
  app,
  systemPreferences,
  shell,
  platform = process.platform,
  osRelease = require("node:os").release(),
  resourcesPath = process.resourcesPath,
  spawn = nodeSpawn,
  compile = compileDevelopmentMacHelper,
  // Chromium's fake capture device (a test run) opens no real microphone.
  fakeCaptureDevice = false,
}) {
  const sessions = new Map();
  let helperPromise = null;

  function unsupportedReason() {
    if (platform !== "darwin") return "On-device dictation needs macOS. Use the local Whisper engine.";
    if (macMajor(osRelease) < 26) return "On-device dictation needs macOS 26 or later. Use the local Whisper engine.";
    return null;
  }

  function helperPath() {
    if (app?.isPackaged) {
      const packaged = path.join(resourcesPath, packagedSpeechHelperPath);
      if (!fs.existsSync(packaged)) {
        return Promise.reject(new Error("This build does not include the on-device dictation helper."));
      }
      return Promise.resolve(packaged);
    }
    helperPromise ||= compile(
      path.join(app.getPath("userData"), "native-speech", "bin", "speech-macos"),
      speechSourcePath,
      [],
    ).catch(error => {
      helperPromise = null;
      throw error;
    });
    return helperPromise;
  }

  function microphoneStatus() {
    if (fakeCaptureDevice || platform !== "darwin" || !systemPreferences?.getMediaAccessStatus) return "granted";
    return systemPreferences.getMediaAccessStatus("microphone");
  }

  async function status() {
    const reason = unsupportedReason();
    if (reason) return { engine: "macos", available: false, reason, microphone: microphoneStatus() };
    try {
      await helperPath();
      return { engine: "macos", available: true, reason: null, microphone: microphoneStatus() };
    } catch (error) {
      return { engine: "macos", available: false, reason: error.message, microphone: microphoneStatus() };
    }
  }

  async function requestMicrophone() {
    const current = microphoneStatus();
    if (current === "granted") return { granted: true, status: current };
    if (current === "denied" || current === "restricted") return { granted: false, status: current };
    const granted = platform === "darwin" && systemPreferences?.askForMediaAccess
      ? await systemPreferences.askForMediaAccess("microphone")
      : true;
    return { granted, status: microphoneStatus() };
  }

  function send(sender, event) {
    if (!sender.isDestroyed()) sender.send("voice:event", event);
  }

  function end(id, kill) {
    const child = sessions.get(id);
    if (!child) return;
    if (kill) child.kill("SIGTERM");
    else child.stdin.end();
  }

  async function start(sender, payload) {
    const reason = unsupportedReason();
    if (reason) return { ok: false, code: "unsupported", error: reason };
    const locale = String(payload?.locale || "en-US");
    if (!LOCALE_PATTERN.test(locale)) return { ok: false, code: "invalid_locale", error: `Unknown language ${locale}.` };
    let executable;
    try {
      executable = await helperPath();
    } catch (error) {
      return { ok: false, code: "helper_missing", error: error.message };
    }
    for (const [id, child] of sessions) {
      if (child.marionetteSender === sender.id) end(id, true);
    }
    const id = `voice-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const child = spawn(executable, ["--locale", locale], { stdio: ["pipe", "pipe", "pipe"] });
    child.marionetteSender = sender.id;
    sessions.set(id, child);
    let buffered = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", chunk => {
      buffered += chunk;
      let newline;
      while ((newline = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, newline).trim();
        buffered = buffered.slice(newline + 1);
        if (!line) continue;
        try {
          send(sender, { id, ...JSON.parse(line) });
        } catch {
          // A line that is not JSON is helper noise, not an event.
        }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-2000); });
    child.stdin.on("error", () => {});
    child.on("error", error => {
      sessions.delete(id);
      send(sender, { id, type: "error", code: "spawn_failed", message: error.message });
    });
    child.on("exit", (code, signal) => {
      sessions.delete(id);
      send(sender, { id, type: "exit", code, signal, stderr: stderr.trim() || undefined });
    });
    return { ok: true, id };
  }

  function write(id, chunk) {
    const child = sessions.get(id);
    if (!child || !child.stdin.writable) return;
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk instanceof ArrayBuffer ? chunk : chunk?.buffer || []);
    if (bytes.length === 0 || bytes.length > MAX_CHUNK_BYTES || bytes.length % 2) return;
    child.stdin.write(bytes);
  }

  function openMicrophoneSettings() {
    if (platform !== "darwin") return Promise.resolve(false);
    return shell.openExternal(MICROPHONE_SETTINGS_URL).then(() => true, () => false);
  }

  function stopAll() {
    for (const id of [...sessions.keys()]) end(id, true);
  }

  return { status, requestMicrophone, start, write, stop: id => end(id, false), cancel: id => end(id, true), openMicrophoneSettings, stopAll };
}

function registerSpeechIpc({ ipcMain, bridge }) {
  ipcMain.handle("voice:status", () => bridge.status());
  ipcMain.handle("voice:requestMicrophone", () => bridge.requestMicrophone());
  ipcMain.handle("voice:openMicrophoneSettings", () => bridge.openMicrophoneSettings());
  ipcMain.handle("voice:start", (event, payload) => bridge.start(event.sender, payload));
  ipcMain.on("voice:chunk", (_event, id, chunk) => bridge.write(String(id), chunk));
  ipcMain.on("voice:stop", (_event, id) => bridge.stop(String(id)));
  ipcMain.on("voice:cancel", (_event, id) => bridge.cancel(String(id)));
}

module.exports = { createSpeechBridge, registerSpeechIpc, macMajor, MICROPHONE_SETTINGS_URL };
