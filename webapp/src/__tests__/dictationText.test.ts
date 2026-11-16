import { describe, expect, it } from "vitest";
import { anchorAt, spliceDictation, stripSendPhrase } from "../lib/dictationText";
import { pcm16ToBase64, toPcm16 } from "../lib/dictationEngine";
import { parseVoiceSettings } from "../lib/voiceSettings";

describe("spliceDictation", () => {
  it("puts spoken text at the cursor with spaces on both sides", () => {
    const anchor = anchorAt("check and report", 5, 5);
    expect(spliceDictation(anchor, "the log")).toEqual({ value: "check the log and report", caret: "check the log".length });
  });

  it("replaces the selection and adds no space before punctuation or after a space", () => {
    expect(spliceDictation(anchorAt("Say WORD.", 4, 8), "hello").value).toBe("Say hello.");
    expect(spliceDictation(anchorAt("", 0, 0), "  hello  ")).toEqual({ value: "hello", caret: 5 });
  });

  it("keeps the box as it was for empty speech", () => {
    expect(spliceDictation(anchorAt("abc", 1, 2), " ")).toEqual({ value: "ac", caret: 1 });
  });

  it("clamps a cursor outside the text", () => {
    expect(anchorAt("abc", 10, 2)).toEqual({ before: "abc", after: "" });
  });
});

describe("stripSendPhrase", () => {
  it.each([
    ["Fix the import, send.", "Fix the import"],
    ["Fix the import and send it", "Fix the import"],
    ["Send", ""],
    ["run the tests. Send!", "run the tests."],
  ])("%s -> %s", (spoken, text) => {
    expect(stripSendPhrase(spoken)).toEqual({ text, send: true });
  });

  it("leaves text that does not end with send", () => {
    expect(stripSendPhrase("send the report to the team")).toEqual({ text: "send the report to the team", send: false });
    expect(stripSendPhrase("check the resend")).toEqual({ text: "check the resend", send: false });
  });
});

describe("audio helpers", () => {
  it("converts float samples to 16 kHz int16 and clamps", () => {
    const out = toPcm16(new Float32Array([0, 1, -1, 2, 0.5, -0.5]), 32_000);
    expect(Array.from(out)).toEqual([0, -32768, 16383]);
  });

  it("encodes little-endian PCM as base64", () => {
    expect(pcm16ToBase64([new Int16Array([1, 256])])).toBe(btoa(String.fromCharCode(1, 0, 0, 1)));
  });
});

describe("parseVoiceSettings", () => {
  it("keeps known values and drops unknown ones", () => {
    expect(parseVoiceSettings({ engine: "whisper", language: "fr-FR", sendOnPhrase: true }))
      .toEqual({ engine: "whisper", language: "fr-FR", sendOnPhrase: true });
    const fallback = parseVoiceSettings({ engine: "cloud", language: "xx", sendOnPhrase: "yes" });
    expect(fallback.language).toBe("en-US");
    expect(fallback.sendOnPhrase).toBe(false);
    expect(["macos", "whisper"]).toContain(fallback.engine);
  });
});
