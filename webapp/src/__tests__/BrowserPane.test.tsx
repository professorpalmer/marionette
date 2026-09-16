// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BrowserPane from "../components/BrowserPane";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("registers ready guests, switches the actual tab, and revokes a closed pane", () => {
  type Context = { sessionId: string; activeTabId: string; tabs: { tabId: string; webContentsId: number }[] };
  let activate: (payload: { sessionId: string; tabId: string }) => void = () => {};
  const setBrowserContext = vi.fn<(context: Context) => Promise<{ ok: boolean }>>().mockResolvedValue({ ok: true });
  vi.stubGlobal("harnessIPC", {
    setBrowserContext,
    onActivateBrowserTab: (callback: typeof activate) => { activate = callback; return () => {}; },
  });
  const view = render(<BrowserPane sessionId="first" />);
  fireEvent.click(screen.getByTitle("New Tab"));
  const guests = view.container.querySelectorAll("webview");
  expect(guests).toHaveLength(2);
  guests.forEach((guest, index) => {
    Object.defineProperty(guest, "getWebContentsId", { value: () => index + 100 });
    fireEvent(guest, new Event("dom-ready"));
  });
  const context = setBrowserContext.mock.calls.at(-1)?.[0];
  expect(context?.tabs).toHaveLength(2);
  expect(context?.sessionId).toBe("first");
  const firstTab = context?.tabs[0].tabId || "";
  act(() => activate({ sessionId: "wrong", tabId: firstTab }));
  expect(guests[0]).toHaveStyle({ display: "none" });
  act(() => activate({ sessionId: "first", tabId: firstTab }));
  expect(guests[0]).toHaveStyle({ display: "flex" });
  expect(setBrowserContext.mock.calls.at(-1)?.[0].activeTabId).toBe(firstTab);
  view.rerender(<BrowserPane sessionId="second" />);
  expect(setBrowserContext.mock.calls.at(-1)?.[0].sessionId).toBe("second");
  view.unmount();
  expect(setBrowserContext.mock.calls.at(-1)?.[0]).toEqual({ sessionId: "", activeTabId: "", tabs: [] });
});
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
