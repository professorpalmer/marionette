import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getOnboardingState,
  resetOnboardingForTests,
  setConfigured,
  setFlow,
  setLocalEndpoint,
  setManual,
  setMode,
  setProviderStatus,
  shouldAutoOpenOnboardingWizard,
  skipFirstRun,
  subscribeOnboarding,
} from "./onboardingStore";

describe("onboardingStore", () => {
  beforeEach(() => {
    localStorage.clear();
    resetOnboardingForTests(false);
  });

  afterEach(() => {
    resetOnboardingForTests(true);
  });

  it("starts with unknown configured and idle flow", () => {
    expect(getOnboardingState()).toMatchObject({
      configured: null,
      flow: "idle",
      mode: "apikey",
      firstRunSkipped: false,
      manual: false,
      localEndpoint: "",
      providerStatusByModel: {},
    });
  });

  it("seeds configured from localStorage", () => {
    localStorage.setItem("pmharness.onboarding.configured", "1");
    resetOnboardingForTests(false);
    expect(getOnboardingState().configured).toBe(true);
  });

  it("seeds firstRunSkipped from localStorage", () => {
    localStorage.setItem("pmharness.onboarding.firstRunSkipped", "1");
    resetOnboardingForTests(false);
    expect(getOnboardingState().firstRunSkipped).toBe(true);
  });

  it("migrates legacy pmharness.wizardSeen into configured and firstRunSkipped", () => {
    localStorage.setItem("pmharness.wizardSeen", "1");
    resetOnboardingForTests(false);
    const state = getOnboardingState();
    expect(state.configured).toBe(true);
    expect(state.firstRunSkipped).toBe(true);
  });

  it("persists skipFirstRun and blocks auto-open", () => {
    skipFirstRun();
    expect(getOnboardingState().firstRunSkipped).toBe(true);
    expect(localStorage.getItem("pmharness.onboarding.firstRunSkipped")).toBe("1");
    expect(localStorage.getItem("pmharness.wizardSeen")).toBe("1");
    expect(shouldAutoOpenOnboardingWizard()).toBe(false);
  });

  it("persists setConfigured and blocks auto-open", () => {
    setConfigured(true);
    expect(getOnboardingState().configured).toBe(true);
    expect(localStorage.getItem("pmharness.onboarding.configured")).toBe("1");
    expect(localStorage.getItem("pmharness.wizardSeen")).toBe("1");
    expect(shouldAutoOpenOnboardingWizard()).toBe(false);
  });

  it("setConfigured(false) records explicit not-configured", () => {
    setConfigured(false);
    expect(getOnboardingState().configured).toBe(false);
    expect(localStorage.getItem("pmharness.onboarding.configured")).toBe("0");
    expect(shouldAutoOpenOnboardingWizard()).toBe(true);
  });

  it("setManual blocks auto-open for the session", () => {
    setManual(true);
    expect(getOnboardingState().manual).toBe(true);
    expect(shouldAutoOpenOnboardingWizard()).toBe(false);
  });

  it("auto-opens when unconfigured and not skipped", () => {
    expect(shouldAutoOpenOnboardingWizard()).toBe(true);
  });

  it("updates flow and mode", () => {
    setFlow("polling");
    setMode("oauth");
    expect(getOnboardingState()).toMatchObject({
      flow: "polling",
      mode: "oauth",
    });
    setFlow("confirming_model");
    expect(getOnboardingState().flow).toBe("confirming_model");
  });

  it("tracks localEndpoint and provider status by model", () => {
    setLocalEndpoint("http://127.0.0.1:11434");
    setProviderStatus("llama3", { ready: true });
    setProviderStatus("mistral", { ready: false });
    expect(getOnboardingState().localEndpoint).toBe("http://127.0.0.1:11434");
    expect(getOnboardingState().providerStatusByModel).toEqual({
      llama3: { ready: true },
      mistral: { ready: false },
    });
  });

  it("notifies subscribers on patch", () => {
    const listener = vi.fn();
    const unsub = subscribeOnboarding(listener);
    expect(listener).toHaveBeenCalledWith(getOnboardingState());

    setFlow("submitting");
    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener.mock.lastCall?.[0].flow).toBe("submitting");

    unsub();
    setFlow("success");
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("resetOnboardingForTests clears storage and rehydrates", () => {
    setConfigured(true);
    skipFirstRun();
    setFlow("error");
    setLocalEndpoint("http://example.test");

    resetOnboardingForTests(true);
    expect(localStorage.getItem("pmharness.onboarding.configured")).toBeNull();
    expect(getOnboardingState()).toMatchObject({
      configured: null,
      flow: "idle",
      firstRunSkipped: false,
      localEndpoint: "",
    });
  });
});
