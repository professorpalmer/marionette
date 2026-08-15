import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { gatherSessionArtifacts } from "../components/conversation/sessionArtifacts";

vi.mock("../lib/api", () => ({
  api: {
    artifacts: vi.fn(),
  },
}));

const mockArtifacts = vi.mocked(api.artifacts);

afterEach(() => {
  vi.clearAllMocks();
});

function displayCards(
  artifacts: Array<{ type: string; headline: string }>,
) {
  return [
    {
      type: "card",
      result: { artifacts },
    },
  ];
}

describe("gatherSessionArtifacts", () => {
  it("returns a sync unique merge when jobIds is missing", () => {
    const result = gatherSessionArtifacts({
      display: displayCards([
        { type: "diff", headline: "a" },
        { type: "diff", headline: "a" },
        { type: "note", headline: "b" },
      ]),
      jobIds: undefined,
      stillCurrent: () => true,
    });

    expect(result).not.toBeInstanceOf(Promise);
    expect(result).toEqual([
      { type: "diff", headline: "a" },
      { type: "note", headline: "b" },
    ]);
    expect(mockArtifacts).not.toHaveBeenCalled();
  });

  it("returns a sync unique merge when jobIds is empty", () => {
    const result = gatherSessionArtifacts({
      display: displayCards([
        { type: "diff", headline: "a" },
        { type: "note", headline: "b" },
        { type: "note", headline: "b" },
      ]),
      jobIds: [],
      stillCurrent: () => true,
    });

    expect(result).not.toBeInstanceOf(Promise);
    expect(result).toEqual([
      { type: "diff", headline: "a" },
      { type: "note", headline: "b" },
    ]);
    expect(mockArtifacts).not.toHaveBeenCalled();
  });

  it("merges display artifacts with fetched job artifacts", async () => {
    mockArtifacts.mockImplementation(async (jobId: string) => {
      if (jobId === "job-1") return [{ type: "patch", headline: "c" }];
      if (jobId === "job-2") return [{ type: "note", headline: "d" }];
      return [];
    });

    const pending = gatherSessionArtifacts({
      display: displayCards([{ type: "diff", headline: "a" }]),
      jobIds: ["job-1", "job-2"],
      stillCurrent: () => true,
    });

    expect(pending).toBeInstanceOf(Promise);
    await expect(pending).resolves.toEqual([
      { type: "diff", headline: "a" },
      { type: "patch", headline: "c" },
      { type: "note", headline: "d" },
    ]);
    expect(mockArtifacts).toHaveBeenCalledWith("job-1");
    expect(mockArtifacts).toHaveBeenCalledWith("job-2");
  });

  it("returns [] when stillCurrent is false after fetches", async () => {
    mockArtifacts.mockResolvedValue([{ type: "patch", headline: "c" }]);

    const result = await gatherSessionArtifacts({
      display: displayCards([{ type: "diff", headline: "a" }]),
      jobIds: ["job-1"],
      stillCurrent: () => false,
    });

    expect(result).toEqual([]);
    expect(mockArtifacts).toHaveBeenCalledWith("job-1");
  });

  it("treats a rejected job fetch as [] and still merges the rest", async () => {
    mockArtifacts.mockImplementation(async (jobId: string) => {
      if (jobId === "job-bad") throw new Error("network");
      return [{ type: "patch", headline: "c" }];
    });
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    await expect(
      gatherSessionArtifacts({
        display: displayCards([{ type: "diff", headline: "a" }]),
        jobIds: ["job-bad", "job-ok"],
        stillCurrent: () => true,
      }),
    ).resolves.toEqual([
      { type: "diff", headline: "a" },
      { type: "patch", headline: "c" },
    ]);

    expect(mockArtifacts).toHaveBeenCalledWith("job-bad");
    expect(mockArtifacts).toHaveBeenCalledWith("job-ok");
    errorSpy.mockRestore();
  });
});
