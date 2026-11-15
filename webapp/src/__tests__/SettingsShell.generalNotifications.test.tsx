import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SettingsShell from "../components/SettingsShell";

vi.mock("../components/GeneralSettingsPage", () => ({
  default: () => <div data-testid="general-page" />,
}));
vi.mock("../components/NotificationsSettingsPage", () => ({
  default: () => <div data-testid="notifications-page" />,
}));

describe("SettingsShell routes General and Notifications to their own pages", () => {
  it.each([
    ["general", "general-page"],
    ["notifications", "notifications-page"],
  ] as const)("%s renders %s", (page, testId) => {
    render(<SettingsShell onClose={vi.fn()} onOpenWizard={vi.fn()} initialPage={page} />);
    expect(screen.getByTestId(testId)).toBeInTheDocument();
  });
});
