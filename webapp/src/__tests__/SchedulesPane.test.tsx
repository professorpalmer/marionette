import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SchedulesPane from "../components/SchedulesPane";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    getSchedules: vi.fn(),
    enableSchedule: vi.fn(),
    disableSchedule: vi.fn(),
    runScheduleNow: vi.fn(),
    getScheduleHistory: vi.fn(),
  },
}));

const getSchedules = vi.mocked(api.getSchedules);
const disableSchedule = vi.mocked(api.disableSchedule);
const getScheduleHistory = vi.mocked(api.getScheduleHistory);

describe("SchedulesPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads schedules on open and shows name/cron/tz/status", async () => {
    getSchedules.mockResolvedValue({
      schedules: [
        {
          id: "abc",
          name: "nightly",
          objective: "audit",
          cron: "0 2 * * *",
          enabled: true,
          timezone: "",
          timezone_mode: "host_local",
          display_status: "never",
          next_fires: ["2024-06-02 02:00"],
        },
      ],
    });

    render(<SchedulesPane />);

    await waitFor(() => {
      expect(screen.getByText("nightly")).toBeInTheDocument();
    });
    expect(screen.getByText(/0 2 \* \* \*/)).toBeInTheDocument();
    expect(screen.getByText(/host-local/)).toBeInTheDocument();
    expect(getSchedules).toHaveBeenCalled();
  });

  it("polls again after disable mutation", async () => {
    getSchedules
      .mockResolvedValueOnce({
        schedules: [
          {
            id: "abc",
            name: "nightly",
            objective: "audit",
            cron: "0 2 * * *",
            enabled: true,
            timezone: "",
            timezone_mode: "host_local",
            display_status: "ok",
          },
        ],
      })
      .mockResolvedValue({
        schedules: [
          {
            id: "abc",
            name: "nightly",
            objective: "audit",
            cron: "0 2 * * *",
            enabled: false,
            timezone: "",
            timezone_mode: "host_local",
            display_status: "ok",
          },
        ],
      });
    disableSchedule.mockResolvedValue({
      id: "abc",
      name: "nightly",
      objective: "audit",
      cron: "0 2 * * *",
      enabled: false,
    });

    render(<SchedulesPane />);
    await waitFor(() => expect(screen.getByText("nightly")).toBeInTheDocument());

    const checkbox = screen.getByTitle("Enable / disable") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(disableSchedule).toHaveBeenCalledWith("abc");
      expect(getSchedules.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("expands history via poll", async () => {
    getSchedules.mockResolvedValue({
      schedules: [
        {
          id: "abc",
          name: "nightly",
          objective: "audit",
          cron: "0 2 * * *",
          enabled: true,
          display_status: "ok",
        },
      ],
    });
    getScheduleHistory.mockResolvedValue({
      id: "abc",
      runs: [{ id: "r1", status: "ok", halt_reason: "objective met and verified" }],
    });

    render(<SchedulesPane />);
    await waitFor(() => expect(screen.getByText("nightly")).toBeInTheDocument());
    fireEvent.click(screen.getByTitle("History"));
    await waitFor(() => {
      expect(getScheduleHistory).toHaveBeenCalledWith("abc", 20);
      expect(screen.getByText(/objective met and verified/)).toBeInTheDocument();
    });
  });
});
