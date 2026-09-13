import { cleanup, renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PendingReview } from "../lib/api";
import { api } from "../lib/api";
import * as reviewExtension from "../components/inFileReviewExtension";
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
          decision_id: "infile_a#0",
          header: "@@ -1,2 +1,2 @@",
          lines: [" keep", "-a", "+b"],
          status: "pending",
        },
        {
          id: "0:1",
          decision_id: "infile_b#0",
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

  it("Accept calls apply_review for only the selected hunk", async () => {
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
      await applyInFileHunkDecision(review, "infile_a#0", "accept");
    });

    expect(api.applyReview).toHaveBeenCalledWith("rev-infile", {
      "infile_a#0": "accept",
    }, "selected");
    expect(reviewHunkDecisionKey("rev-infile", "infile_a#0")).toBe("rev-infile::infile_a#0");
  });

  it("Reject leaves sibling hunks pending", async () => {
    vi.mocked(api.applyReview).mockResolvedValue({
      ok: true,
      message: "ok",
      applied_files: ["src/target.ts"],
      rejected_hunks: ["0:1"],
      checkpoint_id: null,
    } as any);

    const { applyInFileHunkDecision } = await import("../lib/inFileReview");
    await applyInFileHunkDecision(review, "infile_b#0", "reject");

    expect(api.applyReview).toHaveBeenCalledWith("rev-infile", {
      "infile_b#0": "reject",
    }, "selected");
  });
});

it('restarts editor generations, ignores old results, and refreshes on events', async () => {
  vi.useFakeTimers();
  let finish: (rows: PendingReview[]) => void = () => {};
  vi.mocked(api.getReviews).mockReset().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue([]);
  const { result, rerender } = renderHook(({ path }) => useInFileReview(path), { initialProps: { path: 'src/target.ts' } });
  try {
    await act(() => vi.advanceTimersByTimeAsync(12000));
    expect(api.getReviews).toHaveBeenCalledTimes(1);
    rerender({ path: 'other.ts' });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(api.getReviews).toHaveBeenCalledTimes(2);
    rerender({ path: 'src/target.ts' });
    await act(() => vi.advanceTimersByTimeAsync(0));
    await act(async () => finish([review]));
    expect(result.current.pendingCount).toBe(0);
    const calls = vi.mocked(api.getReviews).mock.calls.length;
    await act(async () => window.dispatchEvent(new Event('harness-reviews-refresh')));
    expect(api.getReviews).toHaveBeenCalledTimes(calls + 1);
  } finally { cleanup(); vi.useRealTimers(); }
});


it('refreshes immediately after a successful in-file decision', async () => {
  vi.mocked(api.getReviews).mockReset().mockResolvedValue([review]);
  vi.mocked(api.applyReview).mockResolvedValue({ ok: true, message: "ok", applied_files: [], rejected_hunks: [], checkpoint_id: null });
  const extension = vi.spyOn(reviewExtension, "createInFileReviewExtension").mockReturnValue([]);
  try {
    const { result } = renderHook(() => useInFileReview("src/target.ts"));
    await waitFor(() => expect(result.current.pendingCount).toBe(2));
    const call = extension.mock.calls.at(-1);
    if (!call || !call[0][0]) throw new Error("Expected a pending hunk");
    const [hunks, handlers] = call;
    const before = vi.mocked(api.getReviews).mock.calls.length;
    await act(async () => { await handlers.onAccept(hunks[0]); });
    expect(api.getReviews).toHaveBeenCalledTimes(before + 1);
  } finally { extension.mockRestore(); }
});
