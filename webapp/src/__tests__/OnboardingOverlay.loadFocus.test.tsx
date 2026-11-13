import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useRef, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import OnboardingOverlay from "../components/OnboardingOverlay";
import { api } from "../lib/api";
import { resetOnboardingForTests } from "../state/onboardingStore";
import { FOCUS_COMPOSER_EVENT } from "../lib/sessionNav";

vi.mock("../lib/api", () => ({
  api: {
    providers: vi.fn(),
    setProviderKey: vi.fn(),
    setProviderEnabled: vi.fn(),
    getClaudeCliStatus: vi.fn(),
    getLocalModels: vi.fn(),
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

type Providers = Awaited<ReturnType<typeof api.providers>>;

/** Hold the provider list until the test releases it. */
function deferProviders() {
  let release: (list: Providers) => void = () => {};
  vi.mocked(api.providers).mockReturnValue(new Promise<Providers>((resolve) => { release = resolve; }));
  return (list: Providers) => act(async () => { release(list); });
}

describe("OnboardingOverlay focus after the provider load", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    resetOnboardingForTests(true);
    vi.mocked(api.getClaudeCliStatus).mockResolvedValue({ ok: false, installed: false, authenticated: false });
    vi.mocked(api.getLocalModels).mockResolvedValue({ externals: [] });
  });

  it("moves focus to the recommended key field when nothing is found", async () => {
    const release = deferProviders();
    render(<OnboardingOverlay onClose={vi.fn()} />);
    const later = screen.getByRole("button", { name: /choose a provider later/i });
    await waitFor(() => expect(later).toHaveFocus());
    await release(providers as Providers);
    const field = await screen.findByLabelText("OpenRouter API key");
    await waitFor(() => expect(field).toHaveFocus());
  });

  it("moves focus to the first thing it found", async () => {
    const release = deferProviders();
    render(<OnboardingOverlay onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: /choose a provider later/i })).toHaveFocus());
    await release([...providers, provider("deepseek", { display_name: "DeepSeek", has_env: true, disconnected: true })] as Providers);
    const turnOn = await screen.findByRole("button", { name: "Turn on" });
    await waitFor(() => expect(turnOn).toHaveFocus());
  });

  it("leaves focus alone when the user moved it during the load", async () => {
    const release = deferProviders();
    render(<OnboardingOverlay onClose={vi.fn()} />);
    const later = screen.getByRole("button", { name: /choose a provider later/i });
    await waitFor(() => expect(later).toHaveFocus());
    fireEvent.keyDown(later, { key: "Tab" });
    await release(providers as Providers);
    await screen.findByLabelText("OpenRouter API key");
    await act(async () => { await new Promise((resolve) => requestAnimationFrame(() => resolve(null))); });
    expect(later).toHaveFocus();
  });

  it("announces the loaded state in a polite status region", async () => {
    const release = deferProviders();
    render(<OnboardingOverlay onClose={vi.fn()} />);
    const status = screen.getByTestId("onboarding-load-status");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent("Looking for keys and local models.");
    await release([...providers, provider("deepseek", { display_name: "DeepSeek", has_key: true })] as Providers);
    await waitFor(() => expect(status).toHaveTextContent("Found on this Mac: DeepSeek key."));
  });

  it("announces when nothing is found", async () => {
    const release = deferProviders();
    render(<OnboardingOverlay onClose={vi.fn()} />);
    await release(providers as Providers);
    await waitFor(() => expect(screen.getByTestId("onboarding-load-status"))
      .toHaveTextContent("Nothing found on this Mac. OpenRouter is recommended."));
  });

  describe("focus after Escape", () => {
    /** App shape: the composer listens for the focus event; onboarding mounts over it. */
    function Shell({ opener }: { opener: boolean }) {
      const [open, setOpen] = useState(!opener);
      const ta = useRef<HTMLTextAreaElement>(null);
      useEffect(() => {
        const focus = () => ta.current?.focus();
        window.addEventListener(FOCUS_COMPOSER_EVENT, focus);
        return () => window.removeEventListener(FOCUS_COMPOSER_EVENT, focus);
      }, []);
      return <>
        <textarea ref={ta} aria-label="Message" />
        {opener ? <button type="button" onClick={() => setOpen(true)}>Connect a model</button> : null}
        {open ? <OnboardingOverlay onClose={() => setOpen(false)} /> : null}
      </>;
    }

    it("puts focus in the composer when the app opened onboarding with nothing focused", async () => {
      const release = deferProviders();
      render(<Shell opener={false} />);
      await release(providers as Providers);
      const field = await screen.findByLabelText("OpenRouter API key");
      await waitFor(() => expect(field).toHaveFocus());
      fireEvent.keyDown(field, { key: "Escape" });
      await waitFor(() => expect(screen.queryByTestId("onboarding-overlay")).toBeNull());
      await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus());
    });

    it("returns focus to the control that opened it", async () => {
      const release = deferProviders();
      render(<Shell opener />);
      const opener = screen.getByRole("button", { name: "Connect a model" });
      opener.focus();
      fireEvent.click(opener);
      await release(providers as Providers);
      const field = await screen.findByLabelText("OpenRouter API key");
      await waitFor(() => expect(field).toHaveFocus());
      fireEvent.keyDown(field, { key: "Escape" });
      await waitFor(() => expect(screen.queryByTestId("onboarding-overlay")).toBeNull());
      await waitFor(() => expect(opener).toHaveFocus());
    });
  });
});
