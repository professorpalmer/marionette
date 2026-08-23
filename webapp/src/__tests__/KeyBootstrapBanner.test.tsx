import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import KeyBootstrapBanner from "../components/KeyBootstrapBanner";

describe("KeyBootstrapBanner", () => {
  it("renders issues strip and opens settings", () => {
    const onOpenSettings = vi.fn();
    render(
      <KeyBootstrapBanner
        issues={[{ step: "migrate_legacy", message: "OSError: disk full" }]}
        onOpenSettings={onOpenSettings}
      />,
    );
    expect(screen.getByTestId("key-bootstrap-banner")).toBeTruthy();
    expect(screen.getByText(/did not save on startup/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Open API keys/i }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it("dismisses without calling onOpenSettings", () => {
    const onOpenSettings = vi.fn();
    render(
      <KeyBootstrapBanner
        issues={[{ step: "persist_env_api_keys", message: "PermissionError: x" }]}
        onOpenSettings={onOpenSettings}
      />,
    );
    fireEvent.click(screen.getByTitle("Dismiss"));
    expect(screen.queryByTestId("key-bootstrap-banner")).toBeNull();
    expect(onOpenSettings).not.toHaveBeenCalled();
  });
});
