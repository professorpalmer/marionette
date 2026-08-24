import { describe, expect, it } from "vitest";
import type { ProviderInfo } from "../lib/api";
import {
  FEATURED_OAUTH,
  FEATURED_OAUTH_ORDER,
  defaultOnboardingProvider,
  featuredOnboardingProviders,
  isFeaturedOAuth,
  isOnboardableProvider,
  keyOnboardingProviders,
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

  it("points Z.AI at the Coding Plan key page", () => {
    expect(onboardingCopy("zai").keyUrl).toBe("https://z.ai/manage-apikey/apikey-list");
    expect(onboardingCopy("zai").tagline).toMatch(/coding plan/i);
  });

  it("keeps OpenCode Zen distinct from Go", () => {
    expect(onboardingCopy("opencode-zen").keyUrl).toBe("https://opencode.ai/docs/zen/");
    expect(onboardingCopy("opencode-zen").tagline).toMatch(/ox alpha/i);
    expect(isOnboardableProvider(provider("opencode-zen"))).toBe(true);
  });
});

describe("featured vs key onboarding rows", () => {
  it("features only wired OAuth start handlers", () => {
    expect([...FEATURED_OAUTH_ORDER]).toEqual([
      "openai-codex",
      "anthropic",
      "xai-oauth",
      "nous",
    ]);
    expect(isFeaturedOAuth("qwen-oauth")).toBe(false);
    expect(isFeaturedOAuth("minimax-oauth")).toBe(false);
    expect(FEATURED_OAUTH.has("qwen-oauth")).toBe(false);
    expect(FEATURED_OAUTH.has("minimax-oauth")).toBe(false);
  });

  it("always lists the four featured rows, including synthetic xai-oauth", () => {
    const featured = featuredOnboardingProviders([
      provider("openrouter"),
      provider("anthropic"),
      provider("qwen-oauth"),
      provider("minimax-oauth"),
    ]);
    expect(featured.map((p) => p.name)).toEqual([
      "openai-codex",
      "anthropic",
      "xai-oauth",
      "nous",
    ]);
    expect(featured.some((p) => p.name === "qwen-oauth")).toBe(false);
  });

  it("keeps paste-key rows out of featured OAuth names", () => {
    const keys = keyOnboardingProviders([
      provider("openrouter"),
      provider("anthropic"),
      provider("openai-codex"),
      provider("nous"),
      provider("xai"),
      provider("qwen-oauth"),
    ]);
    expect(keys.map((p) => p.name)).toEqual(["openrouter", "xai", "qwen-oauth"]);
  });
});
