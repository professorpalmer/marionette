import { cleanup, renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PendingReview } from "../lib/api";
import { api } from "../lib/api";
import { useInFileReview } from "../components/useInFileReview";
import { reviewHunkDecisionKey } from "../lib/reviewDecisions";

vi.mock("../lib/api", () => ({
  api: {
    getReviews: vi.fn(),
    applyReview: vi.fn(),
  },
}));

afterEach(() => cleanup());

const review: PendingReview = {
  id: "rev-infile",
  job_id: "job_infileinfile",
  objective: "in-file",
  created_at: 1,
  files: [
    {
      path: "src/target.ts",
      hunks: [
        {
          id: "0:0",
          header: "@@ -1,2 +1,2 @@",
          lines: [" keep", "-a", "+b"],
          status: "pending",
        },
        {
          id: "0:1",
          header: "@@ -8,1 +8,1 @@",
          lines: ["-x", "+y"],
          status: "pending",
        },
      ],
    },
  ],
};

describe("useInFileReview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getReviews).mockResolvedValue([review]);
  });

  it("loads matching pending hunks for the open path", async () => {
    const { result } = renderHook(() => useInFileReview("src/target.ts"));
    await waitFor(() => expect(result.current.pendingCount).toBe(2));
    expect(result.current.extension).toBeTruthy();
  });

  it("Accept calls apply_review with fully seeded namespaced decisions", async () => {
    vi.mocked(api.applyReview).mockResolvedValue({
      ok: true,
      message: "ok",
      applied_files: ["src/target.ts"],
      rejected_hunks: [],
      checkpoint_id: null,
    } as any);

    const { result } = renderHook(() => useInFileReview("src/target.ts"));
    await waitFor(() => expect(result.current.pendingCount).toBe(2));

    // Drive the same helper path the widget buttons use.
    const { applyInFileHunkDecision } = await import("../lib/inFileReview");
    await act(async () => {
      await applyInFileHunkDecision(review, "0:0", "accept");
    });

    expect(api.applyReview).toHaveBeenCalledWith("rev-infile", {
      "0:0": "accept",
      "0:1": "accept",
    });
    expect(reviewHunkDecisionKey("rev-infile", "0:0")).toBe("rev-infile::0:0");
  });

  it("Reject seeds sibling hunks as accept so they are not silently dropped", async () => {
    vi.mocked(api.applyReview).mockResolvedValue({
      ok: true,
      message: "ok",
      applied_files: ["src/target.ts"],
      rejected_hunks: ["0:1"],
      checkpoint_id: null,
    } as any);

    const { applyInFileHunkDecision } = await import("../lib/inFileReview");
    await applyInFileHunkDecision(review, "0:1", "reject");

    expect(api.applyReview).toHaveBeenCalledWith("rev-infile", {
      "0:0": "accept",
      "0:1": "reject",
    });
  });
});
