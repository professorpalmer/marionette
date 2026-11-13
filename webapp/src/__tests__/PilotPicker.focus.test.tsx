import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import PilotPicker from "../components/PilotPicker";
import type { Config } from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: { ...actual.api, swapPilot: vi.fn().mockResolvedValue({}), modelCatalog: vi.fn().mockResolvedValue({ all: [] }) },
  };
});

const config = {
  driver: "openrouter:openai/gpt-6.1-sol",
  reach: "cloud",
  budget: 1,
  models: ["openrouter:openai/gpt-6.1-sol", "openrouter:deepseek/deepseek-v4.1-flash"],
  model_labels: {
    "openrouter:openai/gpt-6.1-sol": "OpenAI: GPT-6.1 Sol",
    "openrouter:deepseek/deepseek-v4.1-flash": "DeepSeek: DeepSeek V4.1 Flash",
  },
  reasoning_effort: "low",
} as unknown as Config;

async function openPicker() {
  const trigger = screen.getByRole("button", { name: /GPT-6.1 Sol/ });
  trigger.focus();
  fireEvent.click(trigger);
  const filter = await screen.findByRole("combobox", { name: "Search models or providers" });
  await waitFor(() => expect(filter).toHaveFocus());
  return { trigger, filter };
}

describe("pilot model picker focus", () => {
  it("returns focus to the trigger after a pick with Enter", async () => {
    render(<><PilotPicker config={config} sessionId="s1" /><button type="button">Outside</button></>);
    const { filter } = await openPicker();
    fireEvent.keyDown(filter, { key: "ArrowDown" });
    fireEvent.keyDown(filter, { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pilot model picker" })).toBeNull());
    await waitFor(() => expect(screen.getByRole("button", { name: /DeepSeek V4.1 Flash/ })).toHaveFocus());
  });

  it("returns focus to the trigger after a pick with a click", async () => {
    render(<PilotPicker config={config} sessionId="" />);
    await openPicker();
    const row = screen.getByRole("option", { name: /DeepSeek V4.1 Flash/ });
    fireEvent.mouseDown(row);
    fireEvent.click(row);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pilot model picker" })).toBeNull());
    await waitFor(() => expect(screen.getByRole("button", { name: /DeepSeek V4.1 Flash/ })).toHaveFocus());
  });

  it("keeps focus on the trigger while App binds the picked model", async () => {
    // App disables model selection while it binds the pick to the session. A
    // native disabled button cannot take focus, so focus fell to BODY.
    function Binding() {
      const [binding, setBinding] = useState(false);
      return <PilotPicker config={config} sessionId="" setupNotice={binding ? "Binding" : undefined}
        modelSelectionDisabled={binding} onPendingModelChange={() => setBinding(true)} />;
    }
    render(<Binding />);
    const { filter } = await openPicker();
    fireEvent.keyDown(filter, { key: "ArrowDown" });
    fireEvent.keyDown(filter, { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pilot model picker" })).toBeNull());
    const trigger = screen.getByRole("button", { name: /DeepSeek V4.1 Flash/ });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(trigger).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(trigger);
    expect(screen.queryByRole("dialog", { name: "Pilot model picker" })).toBeNull();
  });

  it("traps focus: the page behind the open picker is out of the tab order", async () => {
    render(<><PilotPicker config={config} sessionId="s1" /><button type="button">Outside</button></>);
    await openPicker();
    const outside = screen.getByText("Outside");
    await waitFor(() => expect(outside.closest("[inert], [aria-hidden=true]")).not.toBeNull());
    // A hidden close control gives the trap a second stop and touch screen readers a way out.
    expect(screen.getByRole("button", { name: "Close model picker", hidden: true })).toBeInTheDocument();
  });

  it("shows the model name first on the trigger and keeps the vendor in the title", () => {
    render(<PilotPicker config={config} sessionId="s1" />);
    const trigger = screen.getByRole("button", { name: /GPT-6.1 Sol/ });
    expect(trigger.querySelector(".pilot-picker-trigger-label")?.textContent).toBe("GPT-6.1 Sol");
    expect(trigger.getAttribute("title")).toBe("OpenAI: GPT-6.1 Sol (openrouter:openai/gpt-6.1-sol)");
  });
});
