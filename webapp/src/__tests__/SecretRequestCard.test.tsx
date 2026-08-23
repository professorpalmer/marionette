import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  type SecretRequestItem,
} from "../components/TranscriptList";

function pendingSecret(): SecretRequestItem {
  return {
    kind: "secret_request",
    id: "pypi::token",
    label: "PyPI token for puppetmaster-ai",
    connector: "pypi",
    field: "token",
    description: "Project-scoped token for puppetmaster-ai only. Used to twine upload 1.22.20.",
    sessionId: "sess-a",
    status: "pending",
  };
}

function renderCard(onSecretRequest = vi.fn()) {
  render(
    <TranscriptList
      items={[pendingSecret()]}
      status="done"
      compactingStatus={null}
      editingIndex={null}
      auto={false}
      plan={false}
      turnOpen={false}
      scrollContainerRef={{ current: null }}
      onEditMessage={vi.fn()}
      onExecuteSend={vi.fn()}
      onImageClick={vi.fn()}
      onSetCard={vi.fn()}
      onExecutePlan={vi.fn()}
      onCommandApproval={vi.fn()}
      onSecretRequest={onSecretRequest}
    />,
  );
  return onSecretRequest;
}

describe("secret-request card", () => {
  it("renders masked card copy and does not submit on render", () => {
    const decide = renderCard();
    expect(screen.getByText("PyPI token for puppetmaster-ai")).toBeTruthy();
    expect(screen.getByText(/Project-scoped token for puppetmaster-ai only/)).toBeTruthy();
    expect(screen.getByText("Save securely")).toBeTruthy();
    expect(screen.getByText("Stored securely, never shown to your Bot.")).toBeTruthy();
    const input = screen.getByPlaceholderText("Paste your PyPI token for puppetmaster-ai");
    expect(input.getAttribute("type")).toBe("password");
    expect(decide).not.toHaveBeenCalled();
  });

  it("save sends connector/field/value and is not a user transcript submit", () => {
    const decide = renderCard();
    fireEvent.change(screen.getByPlaceholderText("Paste your PyPI token for puppetmaster-ai"), {
      target: { value: "pypi-ui-token-should-not-be-a-user-msg" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save securely" }));
    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({
        connector: "pypi",
        field: "token",
        sessionId: "sess-a",
      }),
      { action: "save", value: "pypi-ui-token-should-not-be-a-user-msg" },
    );
  });

  it("dismiss declines without a value", () => {
    const decide = renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({ connector: "pypi", field: "token" }),
      { action: "dismiss" },
    );
  });
});
