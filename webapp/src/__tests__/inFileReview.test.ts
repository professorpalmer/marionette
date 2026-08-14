import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PendingReview } from "../lib/api";
import { api } from "../lib/api";
import {
  applyInFileHunkDecision,
  buildInFileApplyDecisions,
  collectInFilePendingHunks,
  parseHunkGeometry,
  parseHunkHeader,
} from "../lib/inFileReview";
import {
  reviewHunkDecisionKey,
  seedApplyDecisions,
} from "../lib/reviewDecisions";

vi.mock("../lib/api", () => ({
  api: {
    applyReview: vi.fn(),
    getReviews: vi.fn(),
  },
}));

afterEach(() => {
  vi.clearAllMocks();
});

function makeReview(overrides?: Partial<PendingReview>): PendingReview {
  return {
    id: "rev-a",
    job_id: "job_aaaaaaaaaaaa",
    objective: "patch it",
    created_at: 1,
    files: [
      {
        path: "src/a.ts",
        hunks: [
          {
            id: "0:0",
            header: "@@ -2,3 +2,4 @@",
            lines: [" context", "-old", "+new", " more"],
            status: "pending",
          },
          {
            id: "0:1",
            header: "@@ -20,1 +20,1 @@",
            lines: ["-bye", "+hi"],
            status: "pending",
          },
        ],
      },
      {
        path: "src/b.ts",
        hunks: [
          {
            id: "1:0",
            header: "@@ -1,1 +1,2 @@",
            lines: [" keep", "+extra"],
            status: "pending",
          },
        ],
      },
    ],
    ...overrides,
  };
}

describe("review decision namespace helpers (shared with DiffReviewPane)", () => {
  it("namespaces decisions per review", () => {
    expect(reviewHunkDecisionKey("rev-a", "0:0")).toBe("rev-a::0:0");
    expect(reviewHunkDecisionKey("rev-b", "0:0")).toBe("rev-b::0:0");
  });

  it("seedApplyDecisions fills every hunk id as accept by default", () => {
    const review = makeReview();
    expect(seedApplyDecisions(review, {})).toEqual({
      "0:0": "accept",
      "0:1": "accept",
      "1:0": "accept",
    });
  });
});

describe("parseHunkHeader / parseHunkGeometry", () => {
  it("parses unified hunk headers including omitted counts", () => {
    expect(parseHunkHeader("@@ -10,0 +11,2 @@")).toEqual({
      oldStart: 10,
      oldCount: 0,
      newStart: 11,
      newCount: 2,
    });
    expect(parseHunkHeader("@@ -3 +3 @@")).toEqual({
      oldStart: 3,
      oldCount: 1,
      newStart: 3,
      newCount: 1,
    });
  });

  it("maps body lines onto old-file geometry for paint", () => {
    const geo = parseHunkGeometry({
      id: "0:0",
      header: "@@ -2,3 +2,4 @@",
      lines: [" context", "-old", "+new", " more"],
      status: "pending",
    });
    expect(geo).not.toBeNull();
    expect(geo!.oldLines).toEqual([2, 3, 4]);
    expect(geo!.anchorOldLine).toBe(2);
    expect(geo!.lineKinds.map((l) => l.kind)).toEqual([
      "context",
      "del",
      "add",
      "context",
    ]);
  });

  it("anchors pure inserts after the oldStart line", () => {
    const geo = parseHunkGeometry({
      id: "0:0",
      header: "@@ -10,0 +11,1 @@",
      lines: ["+inserted"],
      status: "pending",
    });
    expect(geo!.oldLines).toEqual([]);
    expect(geo!.anchorOldLine).toBe(10);
  });
});

describe("collectInFilePendingHunks", () => {
  it("returns only hunks whose path matches the open editor", () => {
    const review = makeReview();
    const forA = collectInFilePendingHunks([review], "src/a.ts");
    expect(forA.map((h) => h.hunk.id)).toEqual(["0:0", "0:1"]);
    expect(forA.every((h) => h.decisionKey.startsWith("rev-a::"))).toBe(true);

    const forB = collectInFilePendingHunks([review], "/abs/repo/src/b.ts");
    expect(forB.map((h) => h.hunk.id)).toEqual(["1:0"]);
  });

  it("skips non-pending hunks", () => {
    const review = makeReview();
    review.files[0].hunks[0].status = "accept";
    const forA = collectInFilePendingHunks([review], "src/a.ts");
    expect(forA.map((h) => h.hunk.id)).toEqual(["0:1"]);
  });
});

describe("buildInFileApplyDecisions + applyInFileHunkDecision", () => {
  beforeEach(() => {
    vi.spyOn(window, "dispatchEvent");
  });

  it("Accept seeds all review hunk keys (never omits other files)", () => {
    const review = makeReview();
    expect(buildInFileApplyDecisions(review, "0:0", "accept")).toEqual({
      "0:0": "accept",
      "0:1": "accept",
      "1:0": "accept",
    });
  });

  it("Reject of one hunk still seeds sibling hunks as accept", () => {
    const review = makeReview();
    expect(buildInFileApplyDecisions(review, "0:1", "reject")).toEqual({
      "0:0": "accept",
      "0:1": "reject",
      "1:0": "accept",
    });
  });

  it("applyInFileHunkDecision posts seeded payload and refreshes reviews", async () => {
    vi.mocked(api.applyReview).mockResolvedValue({
      ok: true,
      message: "ok",
      applied_files: ["src/a.ts"],
      rejected_hunks: [],
      checkpoint_id: null,
    } as any);

    const review = makeReview();
    const res = await applyInFileHunkDecision(review, "0:0", "accept");
    expect(res.ok).toBe(true);
    expect(api.applyReview).toHaveBeenCalledWith("rev-a", {
      "0:0": "accept",
      "0:1": "accept",
      "1:0": "accept",
    });

    const kinds = vi.mocked(window.dispatchEvent).mock.calls.map(
      (c) => (c[0] as Event).type,
    );
    expect(kinds).toContain("harness-reviews-refresh");
    expect(kinds).toContain("harness-repo-mutated");
  });

  it("Reject posts namespaced decision without dropping other keys", async () => {
    vi.mocked(api.applyReview).mockResolvedValue({
      ok: true,
      message: "ok",
      applied_files: ["src/a.ts", "src/b.ts"],
      rejected_hunks: ["0:0"],
      checkpoint_id: null,
    } as any);

    const review = makeReview();
    await applyInFileHunkDecision(review, "0:0", "reject");
    expect(api.applyReview).toHaveBeenCalledWith("rev-a", {
      "0:0": "reject",
      "0:1": "accept",
      "1:0": "accept",
    });
  });
});
