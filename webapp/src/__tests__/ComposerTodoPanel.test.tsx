import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ComposerTodoPanel from "../components/conversation/ComposerTodoPanel";
import type { Job } from "../lib/api";
import { clearSessionTodos, publishSessionTodos } from "../lib/sessionTodos";

vi.mock("../lib/api", () => ({
  api: { getSessionState: vi.fn(async () => ({ todos: { phases: [] } })) },
}));

describe("ComposerTodoPanel", () => {
  afterEach(() => {
    clearSessionTodos();
  });

  it("collapses one phase's children without hiding another phase", () => {
    publishSessionTodos({
      phases: [
        { name: "Wave One", tasks: [{ content: "first wave task", status: "pending" }] },
        { name: "Wave Two", tasks: [{ content: "second wave task", status: "pending" }] },
      ],
    }, "sess-waves");

    render(<ComposerTodoPanel sessionId="sess-waves" />);

    const waveOne = screen.getByRole("button", { name: /I\. Wave One/ });
    const waveTwo = screen.getByRole("button", { name: /II\. Wave Two/ });

    expect(waveOne).toHaveAttribute("aria-expanded", "true");
    expect(waveTwo).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("first wave task")).toBeInTheDocument();
    expect(screen.getByText("second wave task")).toBeInTheDocument();

    fireEvent.click(waveOne);
    expect(waveOne).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("first wave task")).not.toBeInTheDocument();
    expect(screen.getByText("second wave task")).toBeInTheDocument();
    expect(waveTwo).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(waveOne);
    expect(waveOne).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("first wave task")).toBeInTheDocument();
    expect(screen.getByText("second wave task")).toBeInTheDocument();
  });

  it("hides todos stamped for a different conversation", () => {
    publishSessionTodos({
      phases: [
        { name: "Wave One", tasks: [{ content: "arena leftover", status: "pending" }] },
      ],
    }, "sess-arena");
    render(<ComposerTodoPanel sessionId="sess-marionette" />);
    expect(screen.queryByText("TODO 0/1")).not.toBeInTheDocument();
    expect(screen.queryByText("arena leftover")).not.toBeInTheDocument();
  });

  it("does not present durable in-progress plan state as a running job", () => {
    publishSessionTodos({
      phases: [
        { name: "Implement", tasks: [{ content: "rewrite the DAG", status: "in_progress" }] },
      ],
    }, "sess-plan");

    render(<ComposerTodoPanel sessionId="sess-plan" />);

    const taskRow = screen.getByText("rewrite the DAG").parentElement;
    expect(taskRow?.querySelector("svg")?.classList.contains("animate-spin")).toBe(false);
  });

  it("animates an in-progress todo only when a live job correlates", () => {
    publishSessionTodos({
      phases: [
        { name: "Implement", tasks: [{ content: "rewrite the DAG", status: "in_progress" }] },
      ],
    }, "sess-plan");
    const job = {
      id: "j-live",
      goal: "rewrite the DAG worker",
      status: "running",
      session_id: "sess-plan",
      tasks: [{ id: "t1", role: "implement", instruction: "rewrite the DAG", status: "running" }],
    } as Job;

    render(<ComposerTodoPanel sessionId="sess-plan" jobs={[job]} />);

    const taskRow = screen.getByText("rewrite the DAG").parentElement;
    expect(taskRow?.querySelector("svg")?.classList.contains("animate-spin")).toBe(true);
  });
});
