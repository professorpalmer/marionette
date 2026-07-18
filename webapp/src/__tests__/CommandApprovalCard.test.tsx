import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  type CommandApprovalItem,
} from "../components/TranscriptList";


function pendingApproval(): CommandApprovalItem {
  return {
    kind: "command_approval",
    id: "call-1",
    command: "ssh prod reboot",
    commandHash: "a".repeat(64),
    sessionId: "session-a",
    workspaceRoot: "/workspace/a",
    category: "remote-shell",
    reason: "remote command execution",
    matched: "ssh",
    status: "pending",
  };
}

function renderApproval(onCommandApproval = vi.fn()) {
  render(
    <TranscriptList
      items={[pendingApproval()]}
      status="done"
      compactingStatus={null}
      editingIndex={null}
      auto
      plan={false}
      turnOpen={false}
      scrollContainerRef={{ current: null }}
      onEditMessage={vi.fn()}
      onExecuteSend={vi.fn()}
      onImageClick={vi.fn()}
      onSetCard={vi.fn()}
      onExecutePlan={vi.fn()}
      onCommandApproval={onCommandApproval}
    />,
  );
  return onCommandApproval;
}


describe("full-auto command approval card", () => {
  it("keeps a destructive command blocked until an explicit decision", () => {
    const decide = renderApproval();

    expect(screen.getByText("Command needs approval")).toBeTruthy();
    expect(screen.getByText(/Full-auto did not run this command/i)).toBeTruthy();
    expect(screen.getByText("ssh prod reboot")).toBeTruthy();
    expect(decide).not.toHaveBeenCalled();
  });

  it("sends the exact pending item for approval", () => {
    const decide = renderApproval();

    fireEvent.click(screen.getByRole("button", { name: "Approve once and retry" }));

    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({
        commandHash: "a".repeat(64),
        sessionId: "session-a",
        workspaceRoot: "/workspace/a",
      }),
      true,
    );
  });

  it("supports rejection without retrying", () => {
    const decide = renderApproval();

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(decide).toHaveBeenCalledWith(expect.any(Object), false);
  });
});
