import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SettingsOptIns, {
  SETTINGS_OPT_IN_KEYS,
  settingsOptInOn,
} from "../components/SettingsOptIns";
import src from "../components/SettingsOptIns.tsx?raw";
import paneSrc from "../components/SettingsPane.tsx?raw";
import shellSrc from "../components/SettingsShell.tsx?raw";
import type { Settings } from "../lib/api";

const sample: Settings = {
  driver: "cursor",
  reach: "repo",
  budget: 10,
  models: [],
  auto_distill: false,
  hash_edit_enabled: false,
  reviewEditsBeforeApply: false,
  autoVerify: true,
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
};

describe("SettingsOptIns", () => {
  it("renders existing Settings keys only", () => {
    render(<SettingsOptIns settings={sample} onUpdate={vi.fn()} />);
    expect(screen.getByTestId("settings-opt-ins")).toBeTruthy();
    expect(screen.getByText("Opt-ins")).toBeTruthy();
    for (const key of SETTINGS_OPT_IN_KEYS) {
      expect(screen.getByTestId(`settings-opt-in-${key}`)).toBeTruthy();
    }
    expect([...SETTINGS_OPT_IN_KEYS]).toEqual([
      "auto_distill",
      "hash_edit_enabled",
      "reviewEditsBeforeApply",
      "autoVerify",
    ]);
  });

  it("reads existing keys and writes the same keys only", () => {
    const onUpdate = vi.fn();
    render(<SettingsOptIns settings={sample} onUpdate={onUpdate} />);
    expect(screen.getByTestId("settings-opt-in-auto_distill").textContent).toMatch(/off/i);
    expect(screen.getByTestId("settings-opt-in-autoVerify").textContent).toMatch(/on/i);

    fireEvent.click(screen.getByTestId("settings-opt-in-auto_distill"));
    expect(onUpdate).toHaveBeenCalledWith({ auto_distill: true });
    fireEvent.click(screen.getByTestId("settings-opt-in-reviewEditsBeforeApply"));
    expect(onUpdate).toHaveBeenCalledWith({ reviewEditsBeforeApply: true });
    fireEvent.click(screen.getByTestId("settings-opt-in-hash_edit_enabled"));
    expect(onUpdate).toHaveBeenCalledWith({ hash_edit_enabled: true });
    fireEvent.click(screen.getByTestId("settings-opt-in-autoVerify"));
    expect(onUpdate).toHaveBeenCalledWith({ autoVerify: false });

    for (const call of onUpdate.mock.calls) {
      const keys = Object.keys(call[0]);
      expect(keys).toHaveLength(1);
      expect(SETTINGS_OPT_IN_KEYS).toContain(keys[0]);
    }
  });

  it("honors defaults when a key is omitted", () => {
    const thin: Settings = {
      driver: "cursor",
      reach: "repo",
      budget: 10,
      models: [],
      auto_distill: false,
      state_dir: "/tmp/state",
      repo: "/tmp/repo",
    };
    expect(settingsOptInOn(thin, "hash_edit_enabled", false)).toBe(false);
    expect(settingsOptInOn(thin, "autoVerify", true)).toBe(true);
  });

  it("stays a Settings section: no new keys, marketplace, Hermes copy, or flag platform", () => {
    expect(src).not.toMatch(/marketplace/i);
    expect(src).not.toMatch(/hermes/i);
    expect(src).not.toMatch(/flag platform|featureFlag|feature_flag/i);
    expect(paneSrc).toMatch(/<SettingsOptIns /);
    expect(paneSrc).toMatch(/onUpdate=\{\(partial\) => \{ void update\(partial\); \}\}/);
    expect(shellSrc).not.toMatch(/SettingsOptIns|opt-ins|marketplace/i);
  });
});
