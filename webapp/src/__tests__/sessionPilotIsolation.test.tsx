import { act, fireEvent, render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PilotPicker from "../components/PilotPicker";
import { api, type Config } from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { ...actual.api, swapPilot: vi.fn().mockResolvedValue({ ok: true }) } };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const config = (driver: string): Config => ({ driver, reach: "cloud", budget: 1, models: ["a", "b", "c"] });

it("does not select a fallback during config hydration", () => {
  render(<PilotPicker config={config("unavailable")} />);
  expect(screen.getByTitle("unavailable")).toBeInTheDocument();
  expect(api.swapPilot).not.toHaveBeenCalled();
});

it.each([[false, false], [true, false], [false, true], [true, true]])("ignores an old picker completion, rejected=%s, returnToA=%s", async (reject, returnToA) => {
  let finish = () => {};
  vi.mocked(api.swapPilot).mockImplementation(() => new Promise((resolve, fail) => {
    finish = () => reject ? fail(new Error("late")) : resolve({ ok: true });
  }));
  const view = render(<PilotPicker key="a" sessionId="a" config={config("a")} />);
  fireEvent.click(screen.getByTitle("a"));
  fireEvent.click(screen.getByTitle("c"));
  expect(api.swapPilot).toHaveBeenCalledWith("c", "a");
  view.rerender(<PilotPicker key="b" sessionId="b" config={config("b")} />);
  if (returnToA) view.rerender(<PilotPicker key="a" sessionId="a" config={config("a")} />);
  const refresh = vi.fn();
  window.addEventListener("harness-config-changed", refresh);
  await act(async () => finish());
  expect(screen.getByTitle(returnToA ? "a" : "b")).toBeInTheDocument();
  expect(refresh).not.toHaveBeenCalled();
  window.removeEventListener("harness-config-changed", refresh);
});
