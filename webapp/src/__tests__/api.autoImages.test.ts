/**
 * Autopilot must forward image attachments the same way chat does — otherwise
 * the UI paints a thumbnail while /api/auto never receives the pixels.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const streamMock = vi.fn(() => () => {});
const postJSONMock = vi.fn(async () => ({ id: "stash1" }));

vi.mock("../lib/transport", () => ({
  getJSON: vi.fn(),
  getJSONSoft: vi.fn(),
  postJSON: (...args: unknown[]) => postJSONMock(...args),
  stream: (...args: unknown[]) => streamMock(...args),
  withToken: (path: string) => path,
  uploadFile: vi.fn(),
  chatEventsPath: vi.fn(),
}));

import { api } from "../lib/api";

describe("api.auto images", () => {
  beforeEach(() => {
    streamMock.mockClear();
    postJSONMock.mockClear();
  });

  it("passes image paths on the /api/auto query string", () => {
    const paths = ["/tmp/uploads/a.png", "/tmp/uploads/b.png"];
    api.auto("look at these", () => {}, undefined, undefined, paths);
    expect(streamMock).toHaveBeenCalledTimes(1);
    const url = String(streamMock.mock.calls[0][0]);
    expect(url).toContain("/api/auto?");
    expect(url).toContain("objective=");
    expect(url).toContain("images=");
    expect(decodeURIComponent(url)).toContain(paths[0]);
    expect(decodeURIComponent(url)).toContain(paths[1]);
  });

  it("stashes objective + images when the payload is large", async () => {
    const big = "x".repeat(5000);
    const paths = ["/tmp/uploads/big.png"];
    api.auto(big, () => {}, undefined, undefined, paths);
    await vi.waitFor(() => expect(postJSONMock).toHaveBeenCalled());
    expect(postJSONMock).toHaveBeenCalledWith("/api/chat/stash", {
      message: big,
      images: paths,
    });
    await vi.waitFor(() => expect(streamMock).toHaveBeenCalled());
    expect(String(streamMock.mock.calls[0][0])).toContain("/api/auto?mid=stash1");
  });
});
