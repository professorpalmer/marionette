import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TranscriptImage } from "../components/conversation/TranscriptImage";
import {
  adoptTranscriptPreviewBlob,
  ownedTranscriptPreviewBlobCount,
  peekOwnedTranscriptPreviewBlob,
  releaseAllTranscriptPreviewBlobs,
  releaseTranscriptPreviewBlob,
  resetTranscriptPreviewBlobsForTests,
} from "../components/conversation/transcriptImageBlobs";
import {
  delayAfterDurableFailure,
  MAX_DURABLE_IMAGE_RETRIES,
  shouldRetryDurableImage,
} from "../components/conversation/transcriptImageRetry";

type ProbeImage = {
  src: string;
  onload: ((ev?: Event) => void) | null;
  onerror: ((ev?: Event) => void) | null;
};

const probes: ProbeImage[] = [];
const revokeSpy = vi.fn();

function lastProbe(): ProbeImage {
  return probes[probes.length - 1];
}

function mockHarnessPort(port: number | string | undefined) {
  const w = window as any;
  if (port === undefined) delete w.__HARNESS_PORT__;
  else w.__HARNESS_PORT__ = port;
}

function mockBackendRespawn(subscribe: ((cb: () => void) => () => void) | null) {
  const w = window as any;
  if (!subscribe) {
    delete w.harnessIPC;
    return;
  }
  w.harnessIPC = { onBackendRespawned: subscribe };
}

beforeEach(() => {
  resetTranscriptPreviewBlobsForTests();
  probes.length = 0;
  revokeSpy.mockReset();
  mockHarnessPort(7788);
  mockBackendRespawn(null);
  vi.stubGlobal(
    "Image",
    class {
      src = "";
      onload: ((ev?: Event) => void) | null = null;
      onerror: ((ev?: Event) => void) | null = null;
      constructor() {
        const self: ProbeImage = this as unknown as ProbeImage;
        probes.push(self);
      }
    },
  );
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: (blob: Blob) => `blob:test-${blob.size || 1}`,
    revokeObjectURL: (url: string) => {
      revokeSpy(url);
    },
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetTranscriptPreviewBlobsForTests();
  mockHarnessPort(undefined);
  mockBackendRespawn(null);
});

describe("transcriptImageRetry helpers", () => {
  it("bounds durable retries and backoff", () => {
    expect(shouldRetryDurableImage({ durablePath: "uploads/a.png", failureCount: 0 })).toBe(true);
    expect(shouldRetryDurableImage({ durablePath: "uploads/a.png", failureCount: MAX_DURABLE_IMAGE_RETRIES - 1 })).toBe(true);
    expect(shouldRetryDurableImage({ durablePath: "uploads/a.png", failureCount: MAX_DURABLE_IMAGE_RETRIES })).toBe(false);
    expect(shouldRetryDurableImage({ durablePath: "blob:x", failureCount: 1 })).toBe(false);
    expect(shouldRetryDurableImage({ durablePath: "https://cdn.example/x.png", failureCount: 1 })).toBe(false);
    expect(delayAfterDurableFailure(1)).toBe(200);
    expect(delayAfterDurableFailure(MAX_DURABLE_IMAGE_RETRIES)).toBe(4000);
    expect(delayAfterDurableFailure(MAX_DURABLE_IMAGE_RETRIES + 1)).toBeNull();
  });
});

describe("transcriptImageBlobs ownership", () => {
  it("revokes on explicit release, not merely by forgetting a mount", () => {
    adoptTranscriptPreviewBlob("uploads/a.png", "blob:owned-1");
    expect(peekOwnedTranscriptPreviewBlob("uploads/a.png")).toBe("blob:owned-1");
    expect(ownedTranscriptPreviewBlobCount()).toBe(1);
    // "unmount" does nothing to the registry
    expect(peekOwnedTranscriptPreviewBlob("uploads/a.png")).toBe("blob:owned-1");
    releaseTranscriptPreviewBlob("uploads/a.png");
    expect(revokeSpy).toHaveBeenCalledWith("blob:owned-1");
    expect(ownedTranscriptPreviewBlobCount()).toBe(0);
  });

  it("releaseAll clears session-discarded blobs", () => {
    adoptTranscriptPreviewBlob("uploads/a.png", "blob:a");
    adoptTranscriptPreviewBlob("uploads/b.png", "blob:b");
    releaseAllTranscriptPreviewBlobs();
    expect(ownedTranscriptPreviewBlobCount()).toBe(0);
    expect(revokeSpy).toHaveBeenCalledWith("blob:a");
    expect(revokeSpy).toHaveBeenCalledWith("blob:b");
  });
});

describe("TranscriptImage resilient surface", () => {
  it("uses blob as immediate fallback while durable establishes (sent + delayed mount)", async () => {
    const onClick = vi.fn();
    const { unmount } = render(
      <TranscriptImage
        path="uploads/shot.png"
        name="shot.png"
        previewUrl="blob:preview-1"
        onImageClick={onClick}
      />,
    );

    const img = screen.getByAltText("shot.png") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("blob:preview-1");
    expect(img.getAttribute("data-blob-fallback")).toBe("1");
    expect(ownedTranscriptPreviewBlobCount()).toBe(1);

    // Offscreen / render-window hide: unmount must NOT revoke the owned blob.
    unmount();
    expect(revokeSpy).not.toHaveBeenCalled();
    expect(peekOwnedTranscriptPreviewBlob("uploads/shot.png")).toBe("blob:preview-1");

    // Late remount still has the blob fallback.
    render(
      <TranscriptImage
        path="uploads/shot.png"
        name="shot.png"
        previewUrl="blob:preview-1"
        onImageClick={onClick}
      />,
    );
    const remounted = screen.getByAltText("shot.png") as HTMLImageElement;
    expect(remounted.getAttribute("src")).toBe("blob:preview-1");
    expect(revokeSpy).not.toHaveBeenCalled();
  });

  it("recovers after first durable failure then success (no blob / reloaded transcript)", async () => {
    vi.useFakeTimers();
    render(
      <TranscriptImage path="uploads/reload.png" name="reload.png" previewUrl="" />,
    );
    const img = screen.getByAltText("reload.png") as HTMLImageElement;
    expect(img.getAttribute("src")).toContain("/api/image?path=");
    expect(img.getAttribute("src")).toContain("7788");

    await act(async () => {
      fireEvent.error(img);
    });
    expect(img.getAttribute("data-failure-count")).toBe("1");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });

    const retried = screen.getByAltText("reload.png") as HTMLImageElement;
    expect(retried.getAttribute("src")).toContain("&_r=1");

    await act(async () => {
      fireEvent.load(retried);
    });
    expect(retried.getAttribute("data-durable-loaded")).toBe("1");
  });

  it("recomputes durable URL from current port on backend respawn before reveal", async () => {
    let respawnCb: (() => void) | null = null;
    mockBackendRespawn((cb) => {
      respawnCb = cb;
      return () => {
        respawnCb = null;
      };
    });

    render(
      <TranscriptImage
        path="uploads/port.png"
        name="port.png"
        previewUrl="blob:port-preview"
      />,
    );
    const img = screen.getByAltText("port.png") as HTMLImageElement;
    expect(img.getAttribute("data-durable-src")).toContain("7788");

    // First probe fails (stale port / busy).
    await act(async () => {
      lastProbe().onerror?.(new Event("error"));
    });

    mockHarnessPort(7799);
    await act(async () => {
      respawnCb?.();
    });

    const after = screen.getByAltText("port.png") as HTMLImageElement;
    expect(after.getAttribute("data-durable-src")).toContain("7799");
    expect(after.getAttribute("data-durable-src")).toContain("&_r=1");
    // Blob still shown until durable succeeds.
    expect(after.getAttribute("src")).toBe("blob:port-preview");

    await act(async () => {
      lastProbe().onload?.(new Event("load"));
    });
    expect(screen.getByAltText("port.png").getAttribute("data-durable-loaded")).toBe("1");
    expect(screen.getByAltText("port.png").getAttribute("src")).toContain("7799");
    expect(revokeSpy).toHaveBeenCalledWith("blob:port-preview");
  });

  it("revokes owned blob only after durable success", async () => {
    const { unmount } = render(
      <TranscriptImage
        path="uploads/ok.png"
        name="ok.png"
        previewUrl="blob:ok-preview"
      />,
    );
    expect(revokeSpy).not.toHaveBeenCalled();

    await act(async () => {
      lastProbe().onload?.(new Event("load"));
    });

    expect(revokeSpy).toHaveBeenCalledTimes(1);
    expect(revokeSpy).toHaveBeenCalledWith("blob:ok-preview");
    expect(ownedTranscriptPreviewBlobCount()).toBe(0);
    const img = screen.getByAltText("ok.png") as HTMLImageElement;
    expect(img.getAttribute("data-durable-loaded")).toBe("1");
    expect(img.getAttribute("src")).toContain("/api/image?path=");

    // Parent rows still carry previewUrl after revoke; remount must not revive it.
    unmount();
    render(
      <TranscriptImage
        path="uploads/ok.png"
        name="ok.png"
        previewUrl="blob:ok-preview"
      />,
    );
    const remounted = screen.getByAltText("ok.png") as HTMLImageElement;
    expect(remounted.getAttribute("src")).toContain("/api/image?path=");
    expect(remounted.getAttribute("data-blob-fallback")).toBe("0");
    expect(ownedTranscriptPreviewBlobCount()).toBe(0);
  });

  it("preserves click/lightbox on the current successful source", async () => {
    const onClick = vi.fn();
    render(
      <TranscriptImage
        path="uploads/click.png"
        name="click.png"
        previewUrl="blob:click-preview"
        onImageClick={onClick}
      />,
    );
    const img = screen.getByAltText("click.png");

    // While durable pending, click uses the visible blob.
    fireEvent.click(img);
    expect(onClick).toHaveBeenCalledWith("blob:click-preview");

    await act(async () => {
      lastProbe().onload?.(new Event("load"));
    });
    fireEvent.click(screen.getByAltText("click.png"));
    const lastUrl = onClick.mock.calls[onClick.mock.calls.length - 1][0] as string;
    expect(lastUrl).toContain("/api/image?path=");
    expect(lastUrl).toContain("click.png");
  });

  it("stops retrying after bounded durable failures", async () => {
    vi.useFakeTimers();
    render(
      <TranscriptImage path="uploads/fail.png" name="fail.png" previewUrl="" />,
    );

    for (let i = 0; i < MAX_DURABLE_IMAGE_RETRIES; i++) {
      const img = screen.getByAltText("fail.png");
      await act(async () => {
        fireEvent.error(img);
      });
      const delay = delayAfterDurableFailure(i + 1);
      if (delay != null) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(delay);
        });
      }
    }

    const finalImg = screen.getByAltText("fail.png") as HTMLImageElement;
    expect(Number(finalImg.getAttribute("data-failure-count"))).toBe(MAX_DURABLE_IMAGE_RETRIES);

    // Extra error must not schedule another generation bump.
    const srcBefore = finalImg.getAttribute("src");
    await act(async () => {
      fireEvent.error(finalImg);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(screen.getByAltText("fail.png").getAttribute("src")).toBe(srcBefore);
    expect(Number(screen.getByAltText("fail.png").getAttribute("data-failure-count"))).toBe(
      MAX_DURABLE_IMAGE_RETRIES,
    );
  });

  it("never retries a pure external source indefinitely", async () => {
    vi.useFakeTimers();
    render(
      <TranscriptImage
        path="https://cdn.example/x.png"
        name="ext.png"
        previewUrl="https://cdn.example/x.png"
      />,
    );
    const img = screen.getByAltText("ext.png");
    await act(async () => {
      fireEvent.error(img);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(img.getAttribute("data-failure-count")).toBe("0");
    expect(probes.length).toBe(0);
  });
});
