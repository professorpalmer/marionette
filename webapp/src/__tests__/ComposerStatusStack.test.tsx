import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ComposerStatusStack from "../components/conversation/ComposerStatusStack";
import { api } from "../lib/api";
import { openAgentCommand } from "../lib/agentLinks";
import { dismissAgentCommandSession } from "../lib/agentCommandIndex";
import type { Job } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: { swarmCancel: vi.fn().mockResolvedValue({ ok: true }) },
}));
vi.mock("../lib/agentLinks", () => ({
  openAgentCommand: vi.fn(),
  openAgentSwarmJob: vi.fn(),
}));
vi.mock("../lib/agentCommandIndex", () => ({
  subscribeAgentCommandIndex: (cb: () => void) => {
    void cb;
    return () => {};
  },
  getAgentCommandIndexVersion: () => 1,
  listAgentCommandSessions: () => [],
  registerAgentCommandSession: () => null,
  dismissAgentCommandSession: vi.fn(() => true),
}));

function commandJob(partial: Partial<Job> & Pick<Job, "id" | "status">): Job {
  return {
    goal: "find ~/.pmharness",
    source: "harness",
    updated_at: Date.now(),
    job_kind: "run_command",
    role: "command",
    adapter: "command",
    command_preview: "find ~/.pmharness",
    ...partial,
  } as Job;
}

describe("ComposerStatusStack", () => {
  beforeEach(() => {
    vi.mocked(api.swarmCancel).mockClear();
    vi.mocked(openAgentCommand).mockClear();
    vi.mocked(dismissAgentCommandSession).mockClear();
  });
  it("collapses settled terminal rows behind a header like the wave bar", () => {
    render(
      <ComposerStatusStack
        swarmJobs={[commandJob({ id: "local-cmd-trunc", status: "truncated" })]}
      />,
    );
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /Open terminal/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Open terminal/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open terminal/ }).className).not.toMatch(/border-edge/);
    expect(screen.queryByRole("button", { name: "Stop command" })).not.toBeInTheDocument();
  });

  it("expands running commands and stops them without opening the terminal", () => {
    render(
      <ComposerStatusStack
        swarmJobs={[commandJob({ id: "local-cmd-live", status: "running" })]}
      />,
    );
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Open terminal/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop command" }));
    expect(dismissAgentCommandSession).toHaveBeenCalledWith("local-cmd-live");
    expect(api.swarmCancel).toHaveBeenCalledWith("local-cmd-live");
    expect(openAgentCommand).not.toHaveBeenCalled();
  });

  it("stops every running command from the group header X while collapsed", () => {
    render(
      <ComposerStatusStack
        swarmJobs={[
          commandJob({ id: "local-cmd-a", status: "running", command_preview: "find a" }),
          commandJob({ id: "local-cmd-b", status: "running", command_preview: "find b" }),
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    expect(screen.getByRole("button", { name: "Terminal" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /Open terminal/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop all commands" }));
    expect(dismissAgentCommandSession).toHaveBeenCalledWith("local-cmd-a");
    expect(dismissAgentCommandSession).toHaveBeenCalledWith("local-cmd-b");
    expect(api.swarmCancel).toHaveBeenCalledWith("local-cmd-a");
    expect(api.swarmCancel).toHaveBeenCalledWith("local-cmd-b");
    expect(openAgentCommand).not.toHaveBeenCalled();
  });
});
