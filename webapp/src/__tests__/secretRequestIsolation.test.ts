import { describe, expect, it } from "vitest";
import {
  appendSecretRequest,
  updateSecretRequest,
} from "../components/conversation/streamApply";
import { transcriptResponseToItems } from "../components/conversation/transcriptItems";
import type { Item } from "../components/TranscriptList";

describe("secret-request isolation", () => {
  it("appends a pending card without a secret value", () => {
    const items = appendSecretRequest([], {
      label: "PyPI token for puppetmaster-ai",
      connector: "pypi",
      field: "token",
      description: "Project-scoped token",
      session_id: "sess-a",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "secret_request",
      connector: "pypi",
      field: "token",
      status: "pending",
    });
    expect(JSON.stringify(items).toLowerCase()).not.toContain("pypi-");
  });

  it("save/dismiss update status in place and never add a user message", () => {
    const pending = appendSecretRequest([], {
      label: "PyPI token for puppetmaster-ai",
      connector: "pypi",
      field: "token",
      session_id: "sess-a",
    });
    const saved = updateSecretRequest(pending, "pypi", "token", { status: "saved" });
    expect(saved.some((i) => i.kind === "msg")).toBe(false);
    expect((saved[0] as Extract<Item, { kind: "secret_request" }>).status).toBe("saved");
    const declined = updateSecretRequest(pending, "pypi", "token", { status: "declined" });
    expect((declined[0] as Extract<Item, { kind: "secret_request" }>).status).toBe("declined");
  });

  it("hydrates display rows without secret bytes", () => {
    const items = transcriptResponseToItems({
      display: [
        {
          type: "secret_request",
          label: "PyPI token for puppetmaster-ai",
          connector: "pypi",
          field: "token",
          description: "help",
          session_id: "sess-a",
          status: "pending",
        },
      ],
    } as any);
    expect(items.some((i) => i.kind === "secret_request")).toBe(true);
    expect(JSON.stringify(items)).not.toContain("value");
  });
});
