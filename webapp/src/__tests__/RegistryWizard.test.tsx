import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RegistryWizard from "../components/RegistryWizard";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    providers: vi.fn(),
    setProviderKey: vi.fn(),
  },
}));

const providers = [
  {
    name: "openrouter",
    display_name: "OpenRouter",
    env_var: "OPENROUTER_API_KEY",
    base_url: "https://openrouter.ai/api/v1",
    has_key: false,
    api_mode: "chat_completions",
    worker_capability: "full_stack" as const,
  },
  {
    name: "anthropic",
    display_name: "Anthropic",
    env_var: "ANTHROPIC_API_KEY",
    base_url: "https://api.anthropic.com",
    has_key: false,
    api_mode: "anthropic_messages",
    worker_capability: "full_stack" as const,
  },
  {
    name: "cursor-cli",
    display_name: "Cursor CLI (plan)",
    env_var: "CURSOR_CLI_LOGIN",
    base_url: "",
    has_key: false,
    api_mode: "cursor_cli",
    worker_capability: "pilot_only" as const,
  },
];

describe("RegistryWizard first-run connect", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.providers).mockResolvedValue(providers);
    vi.mocked(api.setProviderKey).mockResolvedValue({
      ok: true,
      provider: "openrouter",
      has_key: true,
      masked: "sk-or-…",
    });
  });

  it("shows the connect title and defaults to OpenRouter", async () => {
    render(<RegistryWizard onClose={vi.fn()} />);
    expect(await screen.findByText(/set up with Marionette/i)).toBeTruthy();
    expect(screen.getByText(/one key runs chat and swarms/i)).toBeTruthy();
    const openrouter = screen.getByRole("option", { name: /OpenRouter/i });
    expect(openrouter.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText(/Hosts hundreds of models/i)).toBeTruthy();
    expect(screen.queryByText(/Cursor CLI/i)).toBeNull();
  });

  it("switches copy when another provider is selected", async () => {
    render(<RegistryWizard onClose={vi.fn()} />);
    await screen.findByText(/set up with Marionette/i);
    fireEvent.click(screen.getByRole("option", { name: /Anthropic/i }));
    expect(screen.getByText(/Direct Anthropic API/i)).toBeTruthy();
    expect(screen.getByRole("option", { name: /Anthropic/i }).getAttribute("aria-selected")).toBe("true");
  });

  it("saves the selected provider key and closes", async () => {
    const onClose = vi.fn();
    render(<RegistryWizard onClose={onClose} />);
    await screen.findByText(/set up with Marionette/i);
    fireEvent.change(screen.getByPlaceholderText("OPENROUTER_API_KEY"), {
      target: { value: "sk-or-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Connect/i }));
    await waitFor(() => {
      expect(api.setProviderKey).toHaveBeenCalledWith("openrouter", "sk-or-test");
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  it("skips without saving a key", async () => {
    const onClose = vi.fn();
    render(<RegistryWizard onClose={onClose} />);
    await screen.findByText(/set up with Marionette/i);
    fireEvent.click(screen.getByRole("button", { name: /choose a provider later/i }));
    expect(api.setProviderKey).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
