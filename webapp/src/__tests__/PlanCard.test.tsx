import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PlanCard, { PLAN_FULL_LIST_MAX } from "../components/conversation/PlanCard";
import { api, type Job, type SessionTodoSnapshot } from "../lib/api";
import { clearSessionTodos, publishSessionTodos } from "../lib/sessionTodos";

vi.mock("../lib/api", () => ({
  api: {
    getSessionState: vi.fn(async () => ({ todos: { phases: [] } })),
    sessionTodo: vi.fn(async () => ({ ok: true, todos: { phases: [] } })),
  },
}));

const card = () => document.querySelector("[data-slot=plan-card]");
const mode = () => card()?.getAttribute("data-plan-mode") ?? null;

function plan(...statuses: Array<"pending" | "in_progress" | "completed" | "blocked">): SessionTodoSnapshot {
  return {
    phases: [{ name: "Tasks", tasks: statuses.map((status, i) => ({ content: `step ${i + 1}`, status })) }],
  };
}

describe("PlanCard", () => {
  afterEach(() => {
    clearSessionTodos();
    vi.clearAllMocks();
  });

  it("shows the plan the pilot wrote in the open turn as a flat list with no header", () => {
    const { rerender } = render(<PlanCard sessionId="s" active={false} />);
    rerender(<PlanCard sessionId="s" active />);
    expect(card()).toBeNull();

    act(() => publishSessionTodos(plan("completed", "in_progress", "pending"), "s"));

    expect(mode()).toBe("live");
    expect(screen.queryByTestId("plan-card-header")).toBeNull();
    expect(screen.queryByText(/Tasks/)).toBeNull();
    const rows = [...document.querySelectorAll("[data-plan-status]")];
    expect(rows.map((row) => row.getAttribute("data-plan-status"))).toEqual(["completed", "in_progress", "pending"]);
    expect(rows[1]).toHaveAttribute("aria-current", "step");
    expect(rows[0]).not.toHaveAttribute("aria-current");
  });

  it("marks an earlier plan with open steps as from before the last message, until the pilot updates it", () => {
    publishSessionTodos(plan("completed", "in_progress", "pending"), "s");
    const { rerender } = render(<PlanCard sessionId="s" active />);
    expect(mode()).toBe("live");

    rerender(<PlanCard sessionId="s" active={false} />);
    rerender(<PlanCard sessionId="s" active />);
    expect(mode()).toBe("stale");
    expect(screen.getByTestId("plan-card-header")).toHaveTextContent("Plan from before your last message");

    act(() => publishSessionTodos(plan("in_progress", "pending"), "s"));
    expect(mode()).toBe("live");
    expect(screen.queryByTestId("plan-card-header")).toBeNull();
  });

  it("does not bring back a finished plan on the next message", () => {
    publishSessionTodos(plan("completed", "completed"), "s");
    const { rerender } = render(<PlanCard sessionId="s" active />);
    expect(mode()).toBe("live");

    rerender(<PlanCard sessionId="s" active={false} />);
    expect(card()).toBeNull();
    rerender(<PlanCard sessionId="s" active />);
    expect(card()).toBeNull();
  });

  it("keeps a plan the turn left open on screen with its count, and hides one that turn did not write", () => {
    const { rerender } = render(<PlanCard sessionId="s" active={false} />);
    rerender(<PlanCard sessionId="s" active />);
    act(() => publishSessionTodos(plan("completed", "in_progress", "pending"), "s"));
    rerender(<PlanCard sessionId="s" active={false} />);
    expect(mode()).toBe("settled");
    expect(screen.getByTestId("plan-card-header")).toHaveTextContent("Plan · 1 of 3 steps done");
    expect(document.querySelector(".animate-spin")).toBeNull();
  });

  it("hides an old open plan in a session that is idle when it opens", () => {
    publishSessionTodos(plan("blocked", "pending"), "s");
    render(<PlanCard sessionId="s" active={false} />);
    expect(card()).toBeNull();
  });

  it("keeps the plan while an owned background job is live", () => {
    publishSessionTodos({
      phases: [{ name: "Release", tasks: [{ content: "finish installer", status: "in_progress" }] }],
    }, "sess-background");
    const job = {
      id: "j-background",
      goal: "finish installer",
      status: "running",
      session_id: "sess-background",
      tasks: [],
    } as Job;

    render(<PlanCard sessionId="sess-background" jobs={[job]} active={false} />);

    expect(mode()).toBe("live");
  });

  it("dismiss clears the plan of the owning session", () => {
    publishSessionTodos(plan("pending"), "sess-close");
    render(<PlanCard sessionId="sess-close" active />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss plan" }));

    expect(api.sessionTodo).toHaveBeenCalledWith({ command: "/todo clear", session_id: "sess-close" });
    expect(card()).toBeNull();
  });

  it("names the phases only when the plan has more than one", () => {
    publishSessionTodos({
      phases: [
        { name: "Wave One", tasks: [{ content: "first wave task", status: "completed" }] },
        { name: "Wave Two", tasks: [{ content: "second wave task", status: "in_progress" }] },
      ],
    }, "sess-waves");
    render(<PlanCard sessionId="sess-waves" />);

    expect(screen.getByText("Wave One · 1/1")).toBeInTheDocument();
    expect(screen.getByText("Wave Two · 0/1")).toBeInTheDocument();
  });

  it("folds a long plan to its closed lead and next open steps, and opens on request", () => {
    const statuses = Array.from({ length: PLAN_FULL_LIST_MAX + 6 }, (_, i) => (
      i < 4 ? "completed" as const : i === 4 ? "in_progress" as const : "pending" as const
    ));
    publishSessionTodos(plan(...statuses), "s");
    render(<PlanCard sessionId="s" />);

    expect(document.querySelectorAll("[data-plan-status]")).toHaveLength(7);
    expect(screen.queryByText("step 1")).toBeNull();
    expect(screen.getByText("step 5")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Show all 14 steps \(7 more\)/ }));
    expect(document.querySelectorAll("[data-plan-status]")).toHaveLength(14);
    expect(screen.getByRole("button", { name: "Show fewer steps" })).toHaveAttribute("aria-expanded", "true");
  });

  it("hides a plan stamped for a different conversation", () => {
    publishSessionTodos(plan("pending"), "sess-arena");
    render(<PlanCard sessionId="sess-marionette" />);
    expect(card()).toBeNull();
  });

  it("does not present durable in-progress plan state as a running job", () => {
    publishSessionTodos(plan("in_progress"), "sess-plan");
    render(<PlanCard sessionId="sess-plan" />);
    expect(document.querySelector(".animate-spin")).toBeNull();
  });

  it("spins only the step that matches the live wave job", () => {
    publishSessionTodos({
      phases: [
        {
          name: "Wave 1 — Station & Stadium Operations",
          tasks: [
            { content: "Ship station inventory read path", status: "completed" },
            { content: "Implement station management server actions", status: "in_progress" },
            { content: "Validate station ops", status: "pending" },
          ],
        },
        {
          name: "Wave 2 — Live Spectator",
          tasks: [
            { content: "Implement dedicated live spectator TV", status: "pending" },
            { content: "Validate spectator display", status: "pending" },
          ],
        },
        {
          name: "Tasks",
          tasks: [{ content: "implement (agentic) WAVE 1B (Part 1 - Actions only):", status: "pending" }],
        },
      ],
    }, "sess-wave");
    const job = {
      id: "j-wave-1b",
      goal: "WAVE 1B (Part 1 - Actions only)",
      status: "running",
      session_id: "sess-wave",
      role: "implement",
      tasks: [
        { id: "t1", role: "implement", instruction: "Implement station management server actions", status: "running" },
        { id: "t2", role: "implement", instruction: "Implement dedicated live spectator TV", status: "pending" },
      ],
    } as Job;

    render(<PlanCard sessionId="sess-wave" jobs={[job]} />);

    const lit = document.querySelectorAll("[data-todo-lit='1']");
    expect(lit).toHaveLength(1);
    expect(lit[0]?.textContent).toContain("Implement station management server actions");
    expect(document.querySelectorAll(".animate-spin")).toHaveLength(1);
  });
});
