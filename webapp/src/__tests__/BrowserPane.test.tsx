// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BrowserPane from "../components/BrowserPane";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("consumes a pending URL once and preserves existing tabs when harness-open-url arrives", () => {
  vi.stubGlobal("__pmPendingBrowserUrl", "https://pending.example/page");
  vi.stubGlobal("harnessIPC", undefined);
  const view = render(<BrowserPane />);
  const initial = view.container.querySelector('iframe[src="https://duckduckgo.com"]');
  const pending = view.container.querySelector('iframe[src="https://pending.example/page"]');
  expect(initial).toBeTruthy(); expect(pending).toBeTruthy();
  expect(Reflect.get(window, "__pmPendingBrowserUrl")).toBeNull();
  act(() => window.dispatchEvent(new CustomEvent("harness-open-url", { detail: { url: "https://next.example/page" } })));
  expect(view.container.querySelectorAll("iframe")).toHaveLength(3);
  expect(view.container.querySelector('iframe[src="https://pending.example/page"]')).toBe(pending);
  expect(view.container.querySelector('iframe[src="https://duckduckgo.com"]')).toBe(initial);
  expect(screen.getByRole("textbox")).toHaveValue("https://next.example/page");
  fireEvent.click(screen.getByText("pending.example"));
  expect(screen.getByRole("textbox")).toHaveValue("https://pending.example/page");
  view.rerender(<BrowserPane />);
  expect(view.container.querySelectorAll("iframe")).toHaveLength(3);
});
