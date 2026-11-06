/**
 * Autopilot must forward image attachments the same way chat does — otherwise
 * the UI paints a thumbnail while /api/auto never receives the pixels.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const streamMock = vi.fn((..._args: any[]) => () => {});
const postJSONMock = vi.fn(async (..._args: any[]) => ({ id: "stash1" }));

vi.mock("../lib/transport", () => ({
  getJSON: vi.fn(),
  getJSONSoft: vi.fn(),
  postJSON: (...args: any[]) => postJSONMock(...args),
  stream: (...args: any[]) => streamMock(...args),
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
    const url = String(streamMock.mock.calls[0]?.[0]);
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
    expect(String(streamMock.mock.calls[0]?.[0])).toContain("/api/auto?mid=stash1");
  });
});

describe("api auth OAuth helpers", () => {
  beforeEach(() => {
    postJSONMock.mockClear();
  });

  it("post to the /api/auth/oauth routes, not /api/providers/oauth", async () => {
    await api.startAuthOAuth("openai-codex");
    await api.pollAuthOAuth("sess-1", "openai-codex");
    await api.completeAuthOAuth("sess-1", "code-9");
    await api.cancelAuthOAuth("sess-1");
    expect(postJSONMock.mock.calls).toEqual([
      ["/api/auth/oauth/start", { provider: "openai-codex", label: "" }],
      ["/api/auth/oauth/poll", { session_id: "sess-1", provider: "openai-codex" }],
      ["/api/auth/oauth/complete", { session_id: "sess-1", code: "code-9", provider: "anthropic" }],
      ["/api/auth/oauth/cancel", { session_id: "sess-1", provider: "" }],
    ]);
  });
});
