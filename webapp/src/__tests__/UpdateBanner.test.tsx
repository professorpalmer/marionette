import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UpdateBanner from "../components/UpdateBanner";

type CheckResult = {
  available: boolean;
  downloaded: boolean;
};

type ProgressPayload = {
  stage: string;
  message: string;
  percent: number;
};

type ProgressListener = (payload: ProgressPayload) => void;

describe("UpdateBanner update checks", () => {
  let resolveCheck: ((result: CheckResult) => void) | null;
  let rejectCheck: ((error: Error) => void) | null;
  let progressListener: ProgressListener | null;
  let idleListener: (event: Event) => void;

  beforeEach(() => {
    resolveCheck = null;
    rejectCheck = null;
    progressListener = null;
    idleListener = vi.fn((_event: Event) => {});
    window.addEventListener("harness-update-idle", idleListener);

    (window as any).harnessIPC = {
      updates: {
        check: vi.fn(
          () =>
            new Promise<CheckResult>((resolve, reject) => {
              resolveCheck = resolve;
              rejectCheck = reject;
            }),
        ),
        onAvailable: vi.fn(() => () => {}),
        onProgress: vi.fn((listener: ProgressListener) => {
          progressListener = listener;
          return () => {};
        }),
      },
    };
  });

  afterEach(() => {
    window.removeEventListener("harness-update-idle", idleListener);
    delete (window as any).harnessIPC;
  });

  it("clears checking progress when no packaged update is available", async () => {
    render(<UpdateBanner />);

    await waitFor(() => expect(progressListener).not.toBeNull());
    act(() => {
      progressListener?.({
        stage: "check",
        message: "Checking for app shell update",
        percent: 0,
      });
    });
    expect(screen.getByText("Checking for app shell update 0%")).toBeInTheDocument();

    act(() => {
      resolveCheck?.({ available: false, downloaded: false });
    });

    await waitFor(() => {
      expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();
    });
    expect(idleListener).toHaveBeenCalledTimes(1);
  });

  it("clears checking progress when the update check fails", async () => {
    render(<UpdateBanner />);

    await waitFor(() => expect(progressListener).not.toBeNull());
    act(() => {
      progressListener?.({
        stage: "check",
        message: "Checking for app shell update",
        percent: 0,
      });
    });
    expect(screen.getByTestId("update-banner")).toBeInTheDocument();

    act(() => {
      rejectCheck?.(new Error("network unavailable"));
    });

    await waitFor(() => {
      expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();
    });
    expect(idleListener).toHaveBeenCalledTimes(1);
  });
});
