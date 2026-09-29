import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ProviderConfigModal from "../components/ProviderConfigModal";
import {
  changedProviderFields,
  emptyProviderConfig,
  providerConfigFromInfo,
} from "../lib/providerConfig";

afterEach(() => {
  cleanup();
});

const provider = {
  name: "openrouter",
  display_name: "OpenRouter",
  env_var: "OPENROUTER_API_KEY",
  base_url: "https://openrouter.ai/api/v1",
  api_mode: "chat_completions",
  has_key: true,
  masked: "sk-or-••••abcd",
};

function renderModal(overrides: Partial<ComponentProps<typeof ProviderConfigModal>> = {}) {
  const onClose = vi.fn();
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  const result = render(
    <ProviderConfigModal
      open
      provider={provider}
      onClose={onClose}
      onSubmit={onSubmit}
      {...overrides}
    />,
  );
  return { ...result, onClose, onSubmit };
}

describe("changedProviderFields", () => {
  it("submits only edited fields", () => {
    const original = providerConfigFromInfo(provider);
    const draft = { ...original, display_name: "OR" };
    expect(changedProviderFields(original, draft)).toEqual({ display_name: "OR" });
  });

  it("omits an untouched secret and includes it only after retype", () => {
    const original = providerConfigFromInfo(provider);
    expect(original.api_key).toBe("");
    expect(changedProviderFields(original, original, { maskedSecret: provider.masked })).toEqual({});
    expect(
      changedProviderFields(
        original,
        { ...original, api_key: provider.masked },
        { maskedSecret: provider.masked },
      ),
    ).toEqual({});
    expect(
      changedProviderFields(
        original,
        { ...original, api_key: "sk-retyped" },
        { maskedSecret: provider.masked },
      ),
    ).toEqual({ api_key: "sk-retyped" });
  });
});

describe("ProviderConfigModal", () => {
  it("renders grouped fields and seeds secrets blank", async () => {
    renderModal();
    expect(await screen.findByTestId("provider-config-modal")).toBeTruthy();
    expect(screen.getByText("Identity")).toBeTruthy();
    expect(screen.getByText("Credentials")).toBeTruthy();
    expect(screen.getByText("Connection")).toBeTruthy();
    expect((screen.getByTestId("provider-config-field-name") as HTMLInputElement).value).toBe(
      "openrouter",
    );
    expect((screen.getByTestId("provider-config-field-api_key") as HTMLInputElement).value).toBe("");
    expect(screen.getByPlaceholderText("sk-or-••••abcd")).toBeTruthy();
  });

  it("shows descriptive fields read-only so Save cannot drop an edit", async () => {
    renderModal();
    for (const id of ["name", "display_name", "base_url", "api_mode"]) {
      expect((await screen.findByTestId(`provider-config-field-${id}`)).hasAttribute("readonly")).toBe(true);
    }
    expect(screen.getByTestId("provider-config-submit")).toBeDisabled();
  });

  it("submits a retyped key only", async () => {
    const { onSubmit } = renderModal();
    fireEvent.change(await screen.findByTestId("provider-config-field-api_key"), {
      target: { value: "sk-retyped" },
    });
    fireEvent.click(screen.getByTestId("provider-config-submit"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ api_key: "sk-retyped" }));
  });

  it("stays open with the error when saving fails", async () => {
    const { onSubmit, onClose } = renderModal();
    onSubmit.mockRejectedValueOnce(new Error("Unknown provider: acme"));
    fireEvent.change(await screen.findByTestId("provider-config-field-api_key"), {
      target: { value: "sk-new" },
    });
    fireEvent.click(screen.getByTestId("provider-config-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown provider: acme");
    expect(screen.getByTestId("provider-config-modal")).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("adds a key for a known provider chosen from a list", async () => {
    const { onSubmit } = renderModal({ manual: true, provider: null, providerChoices: [{ name: "openai", label: "OpenAI" }, { name: "openrouter", label: "OpenRouter" }] });
    expect(screen.getByRole("option", { name: "OpenAI" })).toBeTruthy();
    const name = await screen.findByTestId("provider-config-field-name");
    expect(name.tagName).toBe("SELECT");
    expect(screen.queryByTestId("provider-config-field-base_url")).toBeNull();
    fireEvent.change(name, { target: { value: "openai" } });
    fireEvent.change(screen.getByTestId("provider-config-field-api_key"), { target: { value: "sk-o" } });
    fireEvent.click(screen.getByTestId("provider-config-submit"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ name: "openai", api_key: "sk-o" }));
  });

  it("opens Add provider with manual=true and empty identity", async () => {
    renderModal({ manual: true, provider: null });
    const dialog = await screen.findByRole("dialog", { name: "Add provider" });
    expect(dialog.getAttribute("data-manual")).toBe("true");
    expect((screen.getByTestId("provider-config-field-name") as HTMLInputElement).value).toBe("");
    expect(emptyProviderConfig().api_key).toBe("");
  });

  it("renders nothing while closed", () => {
    renderModal({ open: false });
    expect(screen.queryByTestId("provider-config-modal")).toBeNull();
  });
});
