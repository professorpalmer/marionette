import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  type CommandApprovalItem,
} from "../components/TranscriptList";


function pendingApproval(overrides: Partial<CommandApprovalItem> = {}): CommandApprovalItem {
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
    ...overrides,
  };
}

function renderApproval(
  item: CommandApprovalItem = pendingApproval(),
  onCommandApproval = vi.fn(),
) {
  render(
    <TranscriptList
      items={[item]}
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
    expect(screen.getByText(/category: remote-shell/i)).toBeTruthy();
    expect(screen.getByText(/matched: ssh/i)).toBeTruthy();
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

  it("shows a third button when a suggested amendment is present", () => {
    const decide = renderApproval(pendingApproval({
      command: "git push --force origin main",
      category: "force-push",
      reason: "history-rewriting git push",
      matched: "git push --force",
      suggestedAmendment: "git push --force-with-lease origin main",
    }));

    expect(screen.getByText(/Suggested safer rewrite/i)).toBeTruthy();
    expect(screen.getByText("git push --force-with-lease origin main")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve suggested amendment" }));
    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({
        suggestedAmendment: "git push --force-with-lease origin main",
      }),
      "amendment",
    );
  });

  it("hides the amendment button when no rewrite exists", () => {
    renderApproval();
    expect(screen.queryByRole("button", { name: "Approve suggested amendment" })).toBeNull();
  });
});
