import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DiffReviewPane, {
  reviewHunkDecisionKey,
  seedApplyDecisions,
} from "../components/DiffReviewPane";
import type { PendingReview } from "../lib/api";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    applyReview: vi.fn(),
    dismissReview: vi.fn(),
  },
}));

afterEach(() => cleanup());

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: true, // reduced motion — skip staggered animation timers
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

describe("DiffReviewPane reviews-load failure honesty", () => {
  it("does not claim empty queue when loadError is set with no reviews", () => {
    render(
      <DiffReviewPane
        reviews={[]}
        onRefresh={vi.fn()}
        loadError="Couldn't load pending reviews."
      />,
    );
    expect(screen.getByTestId("reviews-load-error")).toHaveTextContent(
      /Couldn't load pending reviews/i,
    );
    expect(screen.queryByText(/No pending edits to review/i)).toBeNull();
  });

  it("shows empty queue only when load succeeded with zero reviews", () => {
    render(<DiffReviewPane reviews={[]} onRefresh={vi.fn()} loadError={null} />);
    expect(screen.getByText(/No pending edits to review/i)).toBeTruthy();
    expect(screen.queryByTestId("reviews-load-error")).toBeNull();
  });
});

describe("DiffReviewPane apply decision seeding + namespace", () => {
  it("namespaces decisions per review and seeds all hunks as accept by default", () => {
    const reviewA: PendingReview = {
      id: "rev-a",
      job_id: "job_aaaaaaaaaaaa",
      objective: "a",
      created_at: 1,
      files: [
        {
          path: "a.ts",
          hunks: [{ id: "0:0", header: "@@", lines: ["+a"], status: "pending" }],
        },
      ],
    };
    const reviewB: PendingReview = {
      id: "rev-b",
      job_id: "job_bbbbbbbbbbbb",
      objective: "b",
      created_at: 2,
      files: [
        {
          path: "b.ts",
          hunks: [{ id: "0:0", header: "@@", lines: ["+b"], status: "pending" }],
        },
      ],
    };

    expect(reviewHunkDecisionKey("rev-a", "0:0")).toBe("rev-a::0:0");
    expect(reviewHunkDecisionKey("rev-b", "0:0")).toBe("rev-b::0:0");

    const decisions = {
      [reviewHunkDecisionKey("rev-a", "0:0")]: "reject" as const,
      // rev-b intentionally omitted — seed must paint accept to match UI
    };
    expect(seedApplyDecisions(reviewA, decisions)).toEqual({ "0:0": "reject" });
    expect(seedApplyDecisions(reviewB, decisions)).toEqual({ "0:0": "accept" });
  });

  it("Apply posts a fully seeded payload (no missing keys for harness reject default)", async () => {
    vi.mocked(api.applyReview).mockResolvedValue({
      ok: true,
      message: "ok",
      applied_files: ["a.ts"],
      rejected_hunks: [],
      checkpoint_id: null,
    } as any);

    const reviews: PendingReview[] = [
      {
        id: "rev-seed",
        job_id: "job_seedseedseed",
        objective: "seed me",
        created_at: 1,
        files: [
          {
            path: "a.ts",
            hunks: [
              { id: "0:0", header: "@@ -1 +1 @@", lines: ["+one"], status: "pending" },
              { id: "0:1", header: "@@ -2 +2 @@", lines: ["+two"], status: "pending" },
            ],
          },
        ],
      },
    ];

    render(<DiffReviewPane reviews={reviews} onRefresh={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Apply Selected/i }));

    await vi.waitFor(() => {
      expect(api.applyReview).toHaveBeenCalledWith("rev-seed", {
        "0:0": "accept",
        "0:1": "accept",
      });
    });
  });
});
