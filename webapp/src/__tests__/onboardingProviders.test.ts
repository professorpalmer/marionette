import { describe, expect, it } from "vitest";
import type { ProviderInfo } from "../lib/api";
import {
  defaultOnboardingProvider,
  isOnboardableProvider,
  onboardableProviders,
  onboardingCopy,
} from "../lib/onboardingProviders";

function provider(name: string, extra: Partial<ProviderInfo> = {}): ProviderInfo {
  return {
    name,
    display_name: name,
    env_var: "KEY",
    base_url: "",
    has_key: false,
    api_mode: "chat_completions",
    worker_capability: "full_stack",
    ...extra,
  };
}

describe("onboardingProviders", () => {
  it("hides cursor-cli and bedrock from first-run", () => {
    expect(isOnboardableProvider(provider("cursor-cli", { worker_capability: "pilot_only" }))).toBe(false);
    expect(isOnboardableProvider(provider("bedrock"))).toBe(false);
  });

  it("keeps Full stack providers and sorts OpenRouter first", () => {
    const list = onboardableProviders([
      provider("xai"),
      provider("openrouter"),
      provider("cursor-cli", { worker_capability: "pilot_only" }),
      provider("anthropic"),
    ]);
    expect(list.map((p) => p.name)).toEqual(["openrouter", "anthropic", "xai"]);
  });

  it("defaults to OpenRouter when present", () => {
    expect(defaultOnboardingProvider([provider("anthropic"), provider("openrouter")])).toBe("openrouter");
    expect(defaultOnboardingProvider([provider("xai")])).toBe("xai");
  });

  it("ships a Get-a-key URL for OpenRouter", () => {
    expect(onboardingCopy("openrouter").keyUrl).toBe("https://openrouter.ai/keys");
    expect(onboardingCopy("openrouter").tagline).toMatch(/one key/i);
  });
});
