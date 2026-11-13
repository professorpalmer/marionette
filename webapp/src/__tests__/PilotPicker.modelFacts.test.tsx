import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PilotPicker from "../components/PilotPicker";
import { resetModelFactsCacheForTests } from "../lib/pilotModelFacts";

const modelCatalog = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { ...actual.api, modelCatalog: (...args: unknown[]) => modelCatalog(...args) } };
});

const config = {
  driver: "openrouter:deepseek/deepseek-v4.1-flash",
  reach: "cloud",
  budget: 1,
  models: ["openrouter:deepseek/deepseek-v4.1-flash", "openrouter:z-ai/glm-5.3-flash", "cursor:composer-2.5"],
  model_labels: {
    "openrouter:deepseek/deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "openrouter:z-ai/glm-5.3-flash": "GLM 5.3 Flash",
    "cursor:composer-2.5": "Composer 2.5",
  },
};

afterEach(() => {
  resetModelFactsCacheForTests();
  modelCatalog.mockReset();
});

async function openPicker() {
  render(<PilotPicker config={config} />);
  await act(async () => {
    fireEvent.click(screen.getByTitle(/\(openrouter:deepseek\/deepseek-v4.1-flash\)$/));
  });
}

describe("PilotPicker model facts", () => {
  it("shows price, context and vision per row and marks gaps unknown", async () => {
    modelCatalog.mockResolvedValue({
      catalog: [],
      enabled: [],
      all: [
        { spec: "openrouter:deepseek/deepseek-v4.1-flash", price_in: 0.3, price_out: 1.2, context_window: 1048576, image_input: true },
        { spec: "openrouter:z-ai/glm-5.3-flash", price_in: 0.1, price_out: 0.4, context_window: 202752, image_input: false },
      ],
    });
    await openPicker();
    const option = (name: RegExp) => screen.getByRole("option", { name });
    expect(option(/DeepSeek V4.1 Flash/)).toHaveTextContent("$0.30 / $1.20 per MTok · 1.05M context · Vision");
    expect(option(/GLM 5.3 Flash/)).toHaveTextContent("$0.10 / $0.40 per MTok · 203k context · Text only");
    expect(option(/Composer 2.5/)).toHaveTextContent("Details unknown");
    expect(modelCatalog).toHaveBeenCalledTimes(1);
  });

  it("keeps the picker usable when the catalog read fails", async () => {
    modelCatalog.mockRejectedValue(new Error("offline"));
    await openPicker();
    const facts = screen.getAllByTestId("pilot-model-facts").map((el) => el.textContent);
    expect(facts).toEqual(["Details unknown", "Details unknown", "Details unknown"]);
  });
});
