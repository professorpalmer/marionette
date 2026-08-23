import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ConversationHeader from "../components/conversation/ConversationHeader";
import { TranscriptList, type Item } from "../components/TranscriptList";
import { setCorrelationId } from "../lib/correlationId";

function listProps(items: Item[]) {
  return {
    items,
    status: "done" as const,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: false,
    scrollContainerRef: { current: null },
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

describe("correlation trace chrome", () => {
  it("ConversationHeader shows a copyable trace when the error pill has a correlation id", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    render(
      <ConversationHeader
        pillStatus="error"
        detail="Request failed"
        correlationId="trace-header-1"
      />,
    );

    const trace = screen.getByTestId("trace-copy");
    expect(trace).toHaveTextContent("Trace: trace-header-1");
    fireEvent.click(trace);
    expect(writeText).toHaveBeenCalledWith("trace-header-1");
  });

  it("ConversationHeader hides trace when idle or missing correlation id", () => {
    const { rerender } = render(
      <ConversationHeader pillStatus="error" detail="Request failed" />,
    );
    expect(screen.queryByTestId("trace-copy")).toBeNull();

    rerender(
      <ConversationHeader
        pillStatus="idle"
        correlationId="trace-header-1"
      />,
    );
    expect(screen.queryByTestId("trace-copy")).toBeNull();
  });

  it("AuthFailureBanner shows a copyable trace from the client correlation id", () => {
    setCorrelationId("trace-auth-99");
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    render(
      <TranscriptList
        {...listProps([
          {
            kind: "auth_failure",
            message: "OPENAI_API_KEY rejected",
            id: "auth-1",
          },
        ])}
      />,
    );

    const trace = screen.getByTestId("trace-copy");
    expect(trace).toHaveTextContent("Trace: trace-auth-99");
    fireEvent.click(trace);
    expect(writeText).toHaveBeenCalledWith("trace-auth-99");
    setCorrelationId("");
  });
});
