import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import NotificationsSettingsPage from "../components/NotificationsSettingsPage";
import {
  NOTIFY_PREF_KEY,
  SOUND_PREF_KEY,
  notifyPrefEnabled,
  soundPrefEnabled,
} from "../components/conversation/completionNotify";

function stubNotification(permission: NotificationPermission, next: NotificationPermission = permission) {
  const requestPermission = vi.fn().mockResolvedValue(next);
  vi.stubGlobal("Notification", { permission, requestPermission });
  return requestPermission;
}

describe("NotificationsSettingsPage", () => {
  beforeEach(() => {
    localStorage.removeItem(NOTIFY_PREF_KEY);
    localStorage.removeItem(SOUND_PREF_KEY);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders two rows with the default state", () => {
    stubNotification("granted");
    render(<NotificationsSettingsPage />);
    expect(screen.getByRole("heading", { name: "Notifications" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Desktop notification" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "Sound" })).toHaveAttribute("aria-checked", "false");
  });

  it("writes the keys that the turn-end code reads", () => {
    stubNotification("granted");
    render(<NotificationsSettingsPage />);
    fireEvent.click(screen.getByRole("switch", { name: "Desktop notification" }));
    expect(localStorage.getItem(NOTIFY_PREF_KEY)).toBe("false");
    expect(notifyPrefEnabled()).toBe(false);
    fireEvent.click(screen.getByRole("switch", { name: "Sound" }));
    expect(localStorage.getItem(SOUND_PREF_KEY)).toBe("true");
    expect(soundPrefEnabled()).toBe(true);
    expect(screen.getByRole("switch", { name: "Sound" })).toHaveAttribute("aria-checked", "true");
  });

  it("reads a stored choice", () => {
    stubNotification("granted");
    localStorage.setItem(NOTIFY_PREF_KEY, "false");
    localStorage.setItem(SOUND_PREF_KEY, "true");
    render(<NotificationsSettingsPage />);
    expect(screen.getByRole("switch", { name: "Desktop notification" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("switch", { name: "Sound" })).toHaveAttribute("aria-checked", "true");
  });

  it("asks the system for permission when notifications turn on", async () => {
    const ask = stubNotification("default", "granted");
    localStorage.setItem(NOTIFY_PREF_KEY, "false");
    render(<NotificationsSettingsPage />);
    fireEvent.click(screen.getByRole("switch", { name: "Desktop notification" }));
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(1));
  });

  it("says when the system blocks notifications", () => {
    stubNotification("denied");
    render(<NotificationsSettingsPage />);
    const row = screen.getByTestId("notifications-row-notify");
    expect(within(row).getByText("Blocked in system settings")).toBeInTheDocument();
  });
});
