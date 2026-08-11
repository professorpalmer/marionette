import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DiffReviewPane from "../components/DiffReviewPane";
import type { PendingReview } from "../lib/api";

afterEach(() => cleanup());

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
});

describe("DiffReviewPane sticky PendingReview.error", () => {
  it("surfaces a sticky error banner when a pending review has error set", () => {
    const reviews: PendingReview[] = [
      {
        id: "rev-err001",
        job_id: "job_abcdef012345",
        objective: "ship the patch",
        created_at: 1_700_000_000,
        error: "Failed to apply: conflict in a.ts",
        files: [
          {
            path: "a.ts",
            hunks: [
              {
                id: "h1",
                header: "@@ -1,1 +1,2 @@",
                lines: [" context", "+added"],
                status: "pending",
              },
            ],
          },
        ],
      },
    ];

    render(<DiffReviewPane reviews={reviews} onRefresh={vi.fn()} />);

    const banner = screen.getByTestId("pending-review-error");
    expect(banner).toHaveTextContent(/Last apply failed/i);
    expect(banner).toHaveTextContent(/conflict in a\.ts/i);
    // Review body still paints — error must not look like an empty/idle pane.
    expect(screen.getByText("ship the patch")).toBeTruthy();
    expect(screen.getByText("a.ts")).toBeTruthy();
  });

  it("does not show the sticky error banner when error is absent", () => {
    const reviews: PendingReview[] = [
      {
        id: "rev-ok001",
        job_id: "job_abcdef012345",
        objective: "clean review",
        created_at: 1_700_000_000,
        files: [],
      },
    ];
    render(<DiffReviewPane reviews={reviews} onRefresh={vi.fn()} />);
    expect(screen.queryByTestId("pending-review-error")).toBeNull();
  });
});
