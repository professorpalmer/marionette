import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ComputerAccess from "../components/ComputerAccess";
import { getHarnessIpc } from "../lib/transport";
vi.mock("../lib/transport", () => ({ getHarnessIpc: vi.fn() }));

describe("ComputerAccess", () => {
  it("binds the active session, exposes revoke, and ignores another session's browser request", () => {
    let state: (value: { sessionId: string; apps: { app_id: string; name: string }[]; pending: boolean }) => void = () => {};
    let open: (id: string) => void = () => {};
    const ipc = {
      setComputerSession: vi.fn(), revokeComputerAccess: vi.fn(),
      onComputerState: vi.fn(callback => { state = callback; return vi.fn(); }),
      onOpenBrowser: vi.fn(callback => { open = callback; return vi.fn(); }),
    };
    vi.mocked(getHarnessIpc).mockReturnValue(ipc);
    const onOpen = vi.fn();
    const view = render(<ComputerAccess sessionId="one" onOpenBrowser={onOpen} />);
    expect(ipc.setComputerSession).toHaveBeenCalledWith("one");
    expect(screen.queryByRole("status")).toBeNull();
    act(() => state({ sessionId: "one", apps: [{ app_id: "fixture", name: "Fixture" }], pending: false }));
    expect(screen.getByText("Computer access: Fixture")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop computer control" }));
    expect(ipc.revokeComputerAccess).toHaveBeenCalledOnce();
    open("other");
    expect(onOpen).not.toHaveBeenCalled();
    open("one");
    expect(onOpen).toHaveBeenCalledOnce();
    view.rerender(<ComputerAccess sessionId="two" onOpenBrowser={onOpen} />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(ipc.setComputerSession).toHaveBeenLastCalledWith("two");
    view.unmount();
    expect(ipc.setComputerSession).toHaveBeenLastCalledWith("");
  });
});
