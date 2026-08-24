import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import OnboardingOverlay from "../components/OnboardingOverlay";
import overlaySrc from "../components/OnboardingOverlay.tsx?raw";
import apiSrc from "../lib/api.ts?raw";
import { api } from "../lib/api";
import { FEATURED_OAUTH_ORDER } from "../lib/onboardingProviders";
import {
  getOnboardingState,
  resetOnboardingForTests,
} from "../state/onboardingStore";

vi.mock("../lib/api", () => ({
  api: {
    providers: vi.fn(),
    setProviderKey: vi.fn(),
    startAuthOAuth: vi.fn(),
    pollAuthOAuth: vi.fn(),
    completeAuthOAuth: vi.fn(),
    cancelAuthOAuth: vi.fn(),
  },
}));

function provider(name: string, extra: Record<string, unknown> = {}) {
  return {
    name,
    display_name: name,
    env_var: `${name.toUpperCase()}_API_KEY`,
    base_url: "",
    has_key: false,
    api_mode: "chat_completions",
    worker_capability: "full_stack" as const,
    ...extra,
  };
}

const providers = [
  provider("openrouter", { display_name: "OpenRouter", env_var: "OPENROUTER_API_KEY" }),
  provider("anthropic", { display_name: "Anthropic", env_var: "ANTHROPIC_API_KEY" }),
  provider("openai-codex", { display_name: "ChatGPT Codex (OAuth)", env_var: "OPENAI_CODEX_TOKEN" }),
  provider("nous", { display_name: "Nous Portal (OAuth)", env_var: "NOUS_API_KEY" }),
  provider("xai", { display_name: "xAI Grok", env_var: "XAI_API_KEY" }),
  provider("qwen-oauth", { display_name: "Qwen OAuth" }),
  provider("minimax-oauth", { display_name: "MiniMax OAuth" }),
  provider("cursor-cli", { display_name: "Cursor CLI (plan)", worker_capability: "pilot_only" }),
];

describe("OnboardingOverlay featured vs key rows", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("open", vi.fn());
    localStorage.clear();
    resetOnboardingForTests(true);
    vi.mocked(api.providers).mockResolvedValue(providers);
    vi.mocked(api.setProviderKey).mockResolvedValue({
      ok: true,
      provider: "openrouter",
      has_key: true,
      masked: "sk-or-…",
    });
    vi.mocked(api.startAuthOAuth).mockResolvedValue({
      session_id: "sess-1",
      provider: "openai-codex",
      user_code: "ABCD-1234",
      verification_uri: "https://example.test/device",
    });
    vi.mocked(api.pollAuthOAuth).mockResolvedValue({
      status: "done",
      provider: "openai-codex",
      label: "chatgpt",
    });
    vi.mocked(api.completeAuthOAuth).mockResolvedValue({
      status: "done",
      provider: "anthropic",
      label: "claude-max",
    });
    vi.mocked(api.cancelAuthOAuth).mockResolvedValue({ ok: true });
  });

  it("wires OAuth to existing /api/auth/oauth helpers, not /api/providers/oauth", () => {
    expect(overlaySrc).toContain("startAuthOAuth");
    expect(overlaySrc).toContain("pollAuthOAuth");
    expect(overlaySrc).toContain("completeAuthOAuth");
    expect(overlaySrc).toContain("cancelAuthOAuth");
    expect(overlaySrc).not.toContain("/api/providers/oauth");
    expect(apiSrc).toContain('"/api/auth/oauth/start"');
    expect(apiSrc).toContain('"/api/auth/oauth/poll"');
    expect(apiSrc).toContain('"/api/auth/oauth/complete"');
    expect(apiSrc).toContain('"/api/auth/oauth/cancel"');
    expect(apiSrc).not.toContain("/api/providers/oauth");
    expect(overlaySrc).toContain("FeaturedProviderRow");
    expect(overlaySrc).toContain("KeyProviderRow");
  });

  it("renders featured OAuth rows and key paste rows", async () => {
    render(<OnboardingOverlay onClose={vi.fn()} />);
    expect(await screen.findByTestId("onboarding-overlay")).toBeTruthy();

    const featured = screen.getAllByTestId("featured-provider-row");
    expect(featured.map((el) => el.getAttribute("data-provider"))).toEqual([
      ...FEATURED_OAUTH_ORDER,
    ]);
    expect(screen.getByText("ChatGPT Codex")).toBeTruthy();
    expect(screen.getByText("Claude Max")).toBeTruthy();
    expect(screen.getByText("xAI SuperGrok")).toBeTruthy();
    expect(screen.getByText("Nous Portal")).toBeTruthy();

    const keys = screen.getAllByTestId("key-provider-row");
    const keyNames = keys.map((el) => el.getAttribute("data-provider"));
    expect(keyNames).toContain("openrouter");
    expect(keyNames).toContain("xai");
    expect(keyNames).not.toContain("anthropic");
    expect(keyNames).not.toContain("openai-codex");
    expect(keyNames).not.toContain("nous");
    expect(keyNames).not.toContain("xai-oauth");
    expect(featured.map((el) => el.getAttribute("data-provider"))).not.toContain("qwen-oauth");
    expect(featured.map((el) => el.getAttribute("data-provider"))).not.toContain("minimax-oauth");
    expect(screen.queryByText("Cursor CLI (plan)")).toBeNull();
  });

  it("skip sets firstRunSkipped and closes without saving", async () => {
    const onClose = vi.fn();
    render(<OnboardingOverlay onClose={onClose} />);
    await screen.findByTestId("onboarding-overlay");
    fireEvent.click(screen.getByRole("button", { name: /choose a provider later/i }));
    expect(api.setProviderKey).not.toHaveBeenCalled();
    expect(api.startAuthOAuth).not.toHaveBeenCalled();
    expect(getOnboardingState().firstRunSkipped).toBe(true);
    expect(localStorage.getItem("pmharness.onboarding.firstRunSkipped")).toBe("1");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("key row keeps the paste path via setProviderKey", async () => {
    const onClose = vi.fn();
    render(<OnboardingOverlay onClose={onClose} />);
    await screen.findByTestId("onboarding-overlay");
    fireEvent.change(screen.getByPlaceholderText("OPENROUTER_API_KEY"), {
      target: { value: "sk-or-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Connect/i }));
    await waitFor(() => {
      expect(api.setProviderKey).toHaveBeenCalledWith("openrouter", "sk-or-test");
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(getOnboardingState().configured).toBe(true);
    expect(getOnboardingState().mode).toBe("apikey");
  });

  it("featured Sign in starts the existing auth oauth device flow", async () => {
    const onClose = vi.fn();
    render(<OnboardingOverlay onClose={onClose} />);
    await screen.findByTestId("onboarding-overlay");
    const rows = screen.getAllByTestId("featured-provider-row");
    const codex = rows.find((el) => el.getAttribute("data-provider") === "openai-codex");
    expect(codex).toBeTruthy();
    fireEvent.click(codex!.querySelector("button") as HTMLButtonElement);
    await waitFor(() => {
      expect(api.startAuthOAuth).toHaveBeenCalledWith("openai-codex");
      expect(api.pollAuthOAuth).toHaveBeenCalledWith("sess-1", "openai-codex");
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(getOnboardingState().configured).toBe(true);
    expect(getOnboardingState().mode).toBe("oauth");
  });
});
