import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { useRef } from "react";
import { useOverlayFocus } from "../lib/overlayFocus";

function OverlayTrapFixture({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const firstRef = useRef<HTMLButtonElement>(null);
  useOverlayFocus(open, rootRef, {
    initialFocusRef: firstRef,
    onClose,
    restoreFocus: true,
  });
  if (!open) return null;
  return (
    <div ref={rootRef} role="dialog" aria-label="trap">
      <button ref={firstRef} type="button">
        First
      </button>
      <button type="button">Second</button>
    </div>
  );
}

describe("useOverlayFocus", () => {
  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, "offsetParent", {
      configurable: true,
      get() {
        return this.parentElement;
      },
    });
  });

  it("wraps Tab at the ends and restores focus on Escape", async () => {
    const onClose = vi.fn();
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.textContent = "Trigger";
    document.body.append(trigger);
    trigger.focus();

    const { rerender } = render(<OverlayTrapFixture open onClose={onClose} />);

    const first = screen.getByRole("button", { name: "First" });
    const second = screen.getByRole("button", { name: "Second" });
    await waitFor(() => {
      expect(document.activeElement).toBe(first);
    });

    second.focus();
    expect(document.activeElement).toBe(second);

    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(first);

    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(second);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    await act(async () => {
      rerender(<OverlayTrapFixture open={false} onClose={onClose} />);
    });
    expect(document.activeElement).toBe(trigger);

    trigger.remove();
  });
});
