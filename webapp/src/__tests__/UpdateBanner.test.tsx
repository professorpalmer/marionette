import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UpdateBanner from "../components/UpdateBanner";

type CheckResult = {
  available: boolean;
  downloaded: boolean;
  busy?: boolean;
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

  it("keeps checking progress invisible when no packaged update is available", async () => {
    render(<UpdateBanner />);

    await waitFor(() => expect(progressListener).not.toBeNull());
    act(() => {
      progressListener?.({
        stage: "check",
        message: "Checking for app shell update",
        percent: 0,
      });
    });
    expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();

    act(() => {
      resolveCheck?.({ available: false, downloaded: false });
    });

    await waitFor(() => {
      expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();
    });
    expect(idleListener).toHaveBeenCalledTimes(1);
  });

  it("shows the banner when an update becomes actionable", async () => {
    render(<UpdateBanner />);

    await waitFor(() => expect(progressListener).not.toBeNull());
    act(() => {
      progressListener?.({
        stage: "check",
        message: "Checking for app shell update",
        percent: 0,
      });
      resolveCheck?.({ available: true, downloaded: false });
    });

    await waitFor(() => {
      expect(screen.getByTestId("update-banner")).toBeInTheDocument();
    });
    expect(screen.getByText("A new version of Marionette is ready.")).toBeInTheDocument();
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
    expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();

    act(() => {
      rejectCheck?.(new Error("network unavailable"));
    });

    await waitFor(() => {
      expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();
    });
    expect(idleListener).toHaveBeenCalledTimes(1);
  });

  it("clears checking progress when a terminal idle progress event arrives", async () => {
    render(<UpdateBanner />);

    await waitFor(() => expect(progressListener).not.toBeNull());
    act(() => {
      progressListener?.({
        stage: "check",
        message: "Checking for app shell update",
        percent: 0,
      });
    });
    expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();

    act(() => {
      progressListener?.({ stage: "idle", message: "", percent: 0 });
    });

    await waitFor(() => {
      expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();
    });
    expect(idleListener).toHaveBeenCalledTimes(1);
  });

  it("waits for idle when a concurrent packaged check returns busy", async () => {
    render(<UpdateBanner />);

    await waitFor(() => expect(progressListener).not.toBeNull());
    act(() => {
      progressListener?.({
        stage: "check",
        message: "Checking for app shell update",
        percent: 0,
      });
    });
    expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();

    act(() => {
      resolveCheck?.({ available: false, downloaded: false, busy: true });
    });
    expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();

    act(() => {
      progressListener?.({ stage: "idle", message: "", percent: 0 });
    });

    await waitFor(() => {
      expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument();
    });
    expect(idleListener).toHaveBeenCalledTimes(1);
  });

  it("keeps Installing state when packaged shell install is pending", async () => {
    const apply = vi.fn(async () => ({
      ok: true,
      packagedInstallPending: true,
      installerUpdateRequired: true,
    }));
    (window as any).harnessIPC.updates.apply = apply;
    (window as any).harnessIPC.updates.onAvailable = vi.fn((listener: (res: CheckResult) => void) => {
      listener({ available: true, downloaded: true });
      return () => {};
    });

    render(<UpdateBanner />);

    await waitFor(() => {
      expect(screen.getByTestId("update-banner")).toBeInTheDocument();
    });

    await act(async () => {
      screen.getByRole("button", { name: /restart now/i }).click();
    });

    await waitFor(() => {
      expect(apply).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/Installing app shell/i)).toBeInTheDocument();
    });
    // Must not recover to a second Restart click while quitAndInstall runs.
    expect(screen.queryByRole("button", { name: /restart now/i })).not.toBeInTheDocument();
  });
});
