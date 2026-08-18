import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UpdateBanner from "../components/UpdateBanner";

type CheckResult = {
  available: boolean;
  downloaded: boolean;
  busy?: boolean;
  error?: string;
  behind?: number;
  branch?: string;
  current?: string;
  runtimeStale?: boolean;
  runtimeNote?: string;
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
    vi.restoreAllMocks();
  });

  it("revokes a latched update only after a definitive unavailable check", async () => {
    let now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const onAvailabilityChange = vi.fn();
    const check = vi
      .fn()
      .mockResolvedValueOnce({
        available: true,
        downloaded: false,
        behind: 2,
        branch: "main",
        current: "0.9.245",
      })
      .mockResolvedValueOnce({ available: false, downloaded: false });
    (window as any).harnessIPC.updates.check = check;

    render(<UpdateBanner onAvailabilityChange={onAvailabilityChange} />);

    expect(await screen.findByTestId("update-banner")).toBeInTheDocument();
    expect(onAvailabilityChange).toHaveBeenLastCalledWith({
      behind: 2,
      branch: "main",
      version: "0.9.245",
    });

    now += 5 * 60 * 1000;
    act(() => window.dispatchEvent(new Event("focus")));

    await waitFor(() => expect(check).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument());
    expect(onAvailabilityChange).toHaveBeenLastCalledWith(null);
  });

  it("preserves a latched update across busy and failed checks", async () => {
    let now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const onAvailabilityChange = vi.fn();
    const check = vi
      .fn()
      .mockResolvedValueOnce({
        available: true,
        downloaded: false,
        behind: 1,
        branch: "main",
        current: "0.9.245",
      })
      .mockResolvedValueOnce({ available: false, downloaded: false, busy: true })
      .mockRejectedValueOnce(new Error("network unavailable"));
    (window as any).harnessIPC.updates.check = check;

    render(<UpdateBanner onAvailabilityChange={onAvailabilityChange} />);
    expect(await screen.findByTestId("update-banner")).toBeInTheDocument();

    now += 5 * 60 * 1000;
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(check).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("update-banner")).toBeInTheDocument();

    now += 5 * 60 * 1000;
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(check).toHaveBeenCalledTimes(3));
    expect(screen.getByTestId("update-banner")).toBeInTheDocument();
    expect(onAvailabilityChange).toHaveBeenCalledTimes(1);
  });

  it("preserves a latched update when check resolves a live IPC error", async () => {
    let now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const onAvailabilityChange = vi.fn();
    const check = vi
      .fn()
      .mockResolvedValueOnce({
        available: true,
        downloaded: false,
        behind: 2,
        branch: "main",
        current: "0.9.245",
      })
      .mockResolvedValueOnce({
        available: false,
        downloaded: false,
        error: "fatal: unable to access 'https://github.com/professorpalmer/marionette.git/': Could not resolve host",
      });
    (window as any).harnessIPC.updates.check = check;

    render(<UpdateBanner onAvailabilityChange={onAvailabilityChange} />);
    expect(await screen.findByTestId("update-banner")).toBeInTheDocument();
    expect(onAvailabilityChange).toHaveBeenLastCalledWith({
      behind: 2,
      branch: "main",
      version: "0.9.245",
    });

    now += 5 * 60 * 1000;
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(check).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("update-banner")).toBeInTheDocument();
    expect(onAvailabilityChange).toHaveBeenCalledTimes(1);
    expect(onAvailabilityChange).toHaveBeenLastCalledWith({
      behind: 2,
      branch: "main",
      version: "0.9.245",
    });
  });

  it("surfaces a runtime-stale note from the owner check", async () => {
    const note = "Puppetmaster must be updated before workers are ready.";
    const toast = vi.fn();
    window.addEventListener("harness-toast", toast);
    (window as any).harnessIPC.updates.check = vi.fn().mockResolvedValue({
      available: false,
      downloaded: false,
      runtimeStale: true,
      runtimeNote: note,
    });

    render(<UpdateBanner />);

    await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
    expect((toast.mock.calls[0][0] as CustomEvent).detail).toBe(note);
    window.removeEventListener("harness-toast", toast);
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

  it("clears checking progress when a resolved check carries an error", async () => {
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
      resolveCheck?.({
        available: false,
        downloaded: false,
        error: "git fetch failed",
      });
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

  it("revokes stale availability when apply confirms there is no update", async () => {
    const onAvailabilityChange = vi.fn();
    const apply = vi.fn(async () => ({ ok: false, code: "no_update", error: "no update available" }));
    (window as any).harnessIPC.updates.apply = apply;
    (window as any).harnessIPC.updates.onAvailable = vi.fn((listener: (res: CheckResult) => void) => {
      listener({
        available: true,
        downloaded: false,
        behind: 1,
        branch: "main",
        current: "0.9.245",
      });
      return () => {};
    });

    render(<UpdateBanner onAvailabilityChange={onAvailabilityChange} />);
    await act(async () => screen.getByRole("button", { name: /restart now/i }).click());

    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByTestId("update-banner")).not.toBeInTheDocument());
    expect(onAvailabilityChange).toHaveBeenLastCalledWith(null);
  });

  it("preserves availability when apply-time check fails", async () => {
    const onAvailabilityChange = vi.fn();
    const toast = vi.fn();
    window.addEventListener("harness-toast", toast);
    const apply = vi.fn(async () => ({
      ok: false,
      code: "check_failed",
      error: "fatal: unable to access 'https://github.com/professorpalmer/marionette.git/': Could not resolve host",
    }));
    (window as any).harnessIPC.updates.apply = apply;
    (window as any).harnessIPC.updates.onAvailable = vi.fn((listener: (res: CheckResult) => void) => {
      listener({
        available: true,
        downloaded: false,
        behind: 1,
        branch: "main",
        current: "0.9.245",
      });
      return () => {};
    });

    render(<UpdateBanner onAvailabilityChange={onAvailabilityChange} />);
    expect(await screen.findByTestId("update-banner")).toBeInTheDocument();
    await act(async () => screen.getByRole("button", { name: /restart now/i }).click());

    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("update-banner")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /restart now/i })).toBeInTheDocument();
    expect(onAvailabilityChange).toHaveBeenLastCalledWith({
      behind: 1,
      branch: "main",
      version: "0.9.245",
    });
    expect(onAvailabilityChange).not.toHaveBeenCalledWith(null);
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect((toast.mock.calls[0][0] as CustomEvent).detail).toMatch(/Could not resolve host/);
    window.removeEventListener("harness-toast", toast);
  });
});
