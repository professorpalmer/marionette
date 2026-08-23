import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  activityGroupStableId,
  clearActivityFoldPrefs,
  collapseDuplicateFailedRoutingItems,
  collectIntermediateAssistantItems,
  normalizePlainTextNarration,
  normalizeReasoningPreview,
  resolveActivityGroupOpen,
  type Item,
} from "../components/TranscriptList";
import { _resetAgentCommandIndexForTests } from "../lib/agentCommandIndex";

afterEach(() => cleanup());

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

describe("normalizeReasoningPreview", () => {
  it("strips markdown emphasis markers from the first line", () => {
    expect(normalizeReasoningPreview("**Plan:** check `auth.ts` next\nmore")).toBe(
      "Plan: check auth.ts next",
    );
    expect(normalizeReasoningPreview("*Investigating* __handlers__")).toBe(
      "Investigating handlers",
    );
    // snake_case paths must survive (no single-underscore emphasis strip).
    expect(normalizeReasoningPreview("open auth_handlers.ts")).toBe(
      "open auth_handlers.ts",
    );
  });

  it("preserves ordinary asterisk math/glob text and strips links/strike", () => {
    expect(normalizeReasoningPreview("compute 2*3*4 next")).toBe("compute 2*3*4 next");
    expect(normalizeReasoningPreview("a*b*c")).toBe("a*b*c");
    expect(normalizeReasoningPreview("see [auth.ts](./auth.ts) and ~~old~~")).toBe(
      "see auth.ts and old",
    );
    expect(normalizeReasoningPreview("![diagram](./diag.png) overview")).toBe(
      "diagram overview",
    );
  });

  it("bounds length and ignores later lines", () => {
    const long = `${"a".repeat(200)}\nsecond line`;
    expect(normalizeReasoningPreview(long, 40)).toBe("a".repeat(40));
    expect(normalizeReasoningPreview("first\n**second**")).toBe("first");
  });
});

describe("normalizePlainTextNarration", () => {
  it("strips markdown presentation while preserving line breaks", () => {
    expect(normalizePlainTextNarration("**Plan:**\ncheck `auth.ts`")).toBe(
      "Plan:\ncheck auth.ts",
    );
    expect(normalizePlainTextNarration("## Heading\ncompute 2*3*4")).toBe(
      "Heading\ncompute 2*3*4",
    );
  });
});

describe("transcript presentation contract", () => {
  it("keeps collapsed reasoning sentence-case sans without mono/uppercase/bold chrome", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "look at auth" } },
          {
            kind: "thinking",
            text: "**Plan:** scan auth handlers",
            id: "th-present-1",
          },
        ])}
      />,
    );

    // Reasoning-only turns fold into a quiet activity summary; open it to
    // assert the inner Thought row presentation contract.
    fireEvent.click(screen.getByRole("button", { name: /Plan: scan auth handlers/i }));
    const thought = screen.getByRole("button", { name: /Thought/i });
    const classes = thought.className;
    expect(classes).not.toMatch(/uppercase/);
    expect(classes).not.toMatch(/font-mono/);
    expect(classes).not.toMatch(/tracking-wide/);
    expect(classes).toMatch(/font-sans/);
    expect(classes).toMatch(/font-normal/);
    expect(thought.textContent || "").not.toMatch(/\*\*/);
    expect(within(thought).getByText(/Plan: scan auth handlers/i)).toBeTruthy();
    expect(screen.queryByText(/REASONING/i)).toBeNull();
  });

  it("renders a quiet activity summary row without bordered pill chrome", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "explore" } },
          {
            kind: "thinking",
            text: "mapping files",
            id: "th-act-1",
          },
          {
            kind: "card",
            card: {
              id: "c1",
              goal: "auth.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          {
            kind: "card",
            card: {
              id: "c2",
              goal: "session.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          {
            kind: "codegraph_context",
            symbols: 3,
            query: "auth",
          },
        ])}
      />,
    );

    const summary = screen.getByRole("button", { name: /Explored/i });
    expect(summary.className).not.toMatch(/rounded-lg/);
    expect(summary.className).not.toMatch(/border-edge/);
    expect(summary.className).not.toMatch(/bg-panel2/);
    expect(summary.className).toMatch(/font-sans/);
    expect(summary.getAttribute("aria-expanded")).toBe("false");
    // Secondary CodeGraph badge stays muted and does not dominate the label.
    const cg = within(summary).getByText(/\+ CodeGraph/);
    expect(cg.className).toMatch(/text-faint/);
  });

  it("keeps closed tool rows compact sans normal-weight with aria-expanded", () => {
    const onSetCard = vi.fn();
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "inspect" } },
          {
            kind: "card",
            card: {
              id: "tool-1",
              goal: "auth.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
        ])}
        onSetCard={onSetCard}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Explored/i }));
    // Exactly one keyboard disclosure control per closed tool row.
    const toolDisclosures = screen
      .getAllByRole("button", { expanded: false })
      .filter((el) => /^Read\b/i.test((el.textContent || "").trim()));
    expect(toolDisclosures).toHaveLength(1);
    const toolToggle = toolDisclosures[0];
    expect(toolToggle.className).not.toMatch(/font-mono/);
    expect(toolToggle.className).not.toMatch(/font-medium/);
    expect(toolToggle.className).toMatch(/font-sans/);
    expect(toolToggle.className).toMatch(/font-normal/);
    expect(toolToggle.getAttribute("aria-expanded")).toBe("false");
    // Target link remains a sibling control (not nested inside the expand button).
    expect(screen.getByRole("button", { name: /auth\.ts/i })).toBeTruthy();

    fireEvent.click(toolToggle);
    expect(onSetCard).toHaveBeenCalled();
  });

  it("preserves chronological user → thinking → tools → answer order", () => {
    const { container } = render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "check billing" } },
          { kind: "thinking", text: "billing next", id: "th-order" },
          {
            kind: "card",
            card: {
              id: "card-order",
              goal: "billing.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          { kind: "msg", msg: { role: "assistant", text: "Billing looks fine." } },
        ])}
      />,
    );

    const text = container.textContent || "";
    const userAt = text.indexOf("check billing");
    const exploredAt = text.search(/Explored/i);
    const answerAt = text.indexOf("Billing looks fine.");
    expect(userAt).toBeGreaterThanOrEqual(0);
    expect(exploredAt).toBeGreaterThan(userAt);
    expect(answerAt).toBeGreaterThan(exploredAt);
  });

  it("expanded Thought with **Plan:** renders as regular text without strong/heading chrome", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "look at auth" } },
          {
            kind: "thinking",
            text: "**Plan:**\nscan auth handlers\n\n## Next\nopen session.ts",
            id: "th-plain-1",
          },
        ])}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Plan:/i }));
    const thought = screen.getByRole("button", { name: /Thought/i });
    fireEvent.click(thought);

    expect(screen.queryByRole("strong")).toBeNull();
    expect(document.querySelector("strong")).toBeNull();
    expect(document.querySelector("h1, h2, h3, h4, h5, h6")).toBeNull();
    // Expanded Thought strips Markdown chrome then renders via the shared
    // Markdown linker so paths/URLs stay clickable (no inert <pre>).
    const body = thought.closest("div")?.querySelector(".border-l-2");
    expect(body?.textContent || "").toContain("Plan:");
    expect(body?.textContent || "").toContain("scan auth handlers");
    expect(body?.textContent || "").not.toMatch(/\*\*/);
  });

  it("folded isPlan narration is regular text inside the investigation group", () => {
    const planMsg: Item = {
      kind: "msg",
      msg: {
        role: "assistant",
        text: "**Plan:** retry routing after vision fix",
        isPlan: true,
      },
    };
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "fix swarm" } },
          planMsg,
          {
            kind: "thinking",
            text: "checking registry aliases",
            id: "th-plan-fold",
          },
          {
            kind: "card",
            card: {
              id: "card-plan-fold",
              goal: "marionette_registry.py",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          { kind: "msg", msg: { role: "assistant", text: "Routing is fixed." } },
        ])}
      />,
    );

    // Sealed isPlan with later tools must fold (not stand alone as Markdown).
    const intermediate = collectIntermediateAssistantItems(
      [
        { kind: "msg", msg: { role: "user", text: "fix swarm" } },
        planMsg,
        {
          kind: "thinking",
          text: "checking registry aliases",
          id: "th-plan-fold",
        },
        {
          kind: "card",
          card: {
            id: "card-plan-fold",
            goal: "marionette_registry.py",
            cwd: null,
            kind: "read_file",
            running: false,
            open: false,
            result: { status: "ok" },
          },
        },
        { kind: "msg", msg: { role: "assistant", text: "Routing is fixed." } },
      ],
      false,
    );
    expect(intermediate.has(planMsg)).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /Explored/i }));
    expect(document.querySelector("strong")).toBeNull();
    const folded = screen.getByText(/Plan: retry routing after vision fix/i);
    expect(folded.tagName.toLowerCase()).toBe("pre");
    expect(folded.className).toMatch(/font-normal/);
    expect(folded.textContent || "").not.toMatch(/\*\*/);
    // Final answer remains Markdown-capable outside the fold.
    expect(screen.getByText(/Routing is fixed/i)).toBeTruthy();
  });

  it("standalone isPlan Bubble uses plain-text narration (not Markdown strong)", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "plan the fix" } },
          {
            kind: "msg",
            msg: {
              role: "assistant",
              text: "**Plan:**\n1. tag vision\n2. retry swarm",
              isPlan: true,
            },
          },
        ])}
      />,
    );

    expect(document.querySelector("strong")).toBeNull();
    const planBody = screen.getByText(/Plan:/i);
    expect(planBody.tagName.toLowerCase()).toBe("pre");
    expect(planBody.className).toMatch(/font-normal/);
    expect(planBody.textContent || "").toContain("1. tag vision");
    expect(planBody.textContent || "").not.toMatch(/\*\*/);
    // User bubble stays plain whitespace-pre-wrap (unchanged path).
    expect(screen.getByText("plan the fix")).toBeTruthy();
  });

  it("collapses paired failed run_swarm card + swarm_result and keeps a distinct failure", () => {
    const routingError =
      "auto-route failed: No model in registry has all required tags ['vision']";
    const objective = "Audit the failure shown in the latest screenshot";
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit screenshot" } },
      {
        kind: "card",
        card: {
          id: "swarm-a",
          goal: objective,
          cwd: null,
          kind: "run_swarm",
          running: false,
          open: false,
          result: { error: routingError },
        },
      },
      {
        kind: "card",
        card: {
          id: "swarm-b",
          goal: objective,
          cwd: null,
          kind: "run_swarm",
          running: false,
          open: false,
          result: { error: routingError },
        },
      },
      {
        kind: "card",
        card: {
          id: "swarm-c",
          goal: objective,
          cwd: null,
          kind: "run_swarm",
          running: false,
          open: false,
          result: { error: routingError },
        },
      },
      {
        kind: "swarm_result",
        job_id: "job-dup-1",
        applied: false,
        files: [],
        summary: "",
        error: routingError,
        objective,
      },
      {
        kind: "swarm_result",
        job_id: "job-dup-2",
        applied: false,
        files: [],
        summary: "",
        error: routingError,
        objective,
      },
      {
        kind: "swarm_result",
        job_id: "job-distinct",
        applied: false,
        files: [],
        summary: "",
        error: "worker timed out after 30s",
        objective: "Distinct timeout failure",
      },
    ];

    const activity = items.filter(
      (it): it is Extract<Item, { kind: "card" | "swarm_result" }> =>
        it.kind === "card" || it.kind === "swarm_result",
    );
    const collapsed = collapseDuplicateFailedRoutingItems(activity);
    // Paired ActionCard + terminal swarm_result share one fingerprint.
    expect(collapsed.items).toHaveLength(2); // 1 routing cluster + 1 distinct
    expect(collapsed.duplicateCounts[0]).toBe(5);
    expect(collapsed.duplicateCounts[1]).toBe(1);
    expect(collapsed.items[0]).toMatchObject({
      kind: "swarm_result",
      error: routingError,
      objective,
    });
    expect(collapsed.items[1]).toMatchObject({
      kind: "swarm_result",
      error: "worker timed out after 30s",
    });

    render(<TranscriptList {...listProps(items)} />);

    expect(screen.getByText(/worker timed out after 30s/i)).toBeTruthy();
    // ActionCards are no longer terminal owners; each durable job result remains distinct.
    const failedLabels = screen.getAllByText(/swarm failed/i);
    expect(failedLabels).toHaveLength(3);
    expect(screen.getAllByText(/No model in registry has all required tags/i)).toHaveLength(2);
  });

  it("paints held_for_review and analysis_ok badges without applied/failed chrome", () => {
    render(
      <TranscriptList
        {...listProps([
          {
            kind: "swarm_result",
            job_id: "job_heldabcdef01",
            applied: false,
            files: ["a.ts"],
            summary: "Patch held for review",
            error: null,
            objective: "ship patch",
            held_for_review: true,
          },
          {
            kind: "swarm_result",
            job_id: "job_analysisok02",
            applied: false,
            files: [],
            summary: "FINDING: race",
            error: null,
            objective: "audit auth",
            analysis_ok: true,
          },
          {
            kind: "swarm_result",
            job_id: "job_appliedok003",
            applied: true,
            files: ["b.ts"],
            summary: "ok",
            error: null,
            objective: "landed",
          },
          {
            kind: "pending_review",
            id: "rev-deadbeef",
            summary: "Held 1 files for review",
          },
        ])}
      />,
    );

    const cards = screen.getAllByTestId("swarm-result-card");
    const byOutcome = Object.fromEntries(
      cards.map((el) => [el.getAttribute("data-outcome"), el]),
    );
    expect(byOutcome.held).toBeTruthy();
    expect(byOutcome.held).toHaveTextContent(/held for review/i);
    expect(byOutcome.analysis).toBeTruthy();
    expect(byOutcome.analysis).toHaveTextContent(/analysis done/i);
    expect(byOutcome.applied).toBeTruthy();
    expect(byOutcome.applied).toHaveTextContent(/swarm done/i);
    expect(byOutcome.failed).toBeUndefined();

    expect(screen.getByTestId("pending-review-receipt")).toHaveTextContent(/review ready/i);
  });

  it("pending_review receipt click focuses Review tab and refreshes", () => {
    const focusSpy = vi.fn();
    const refreshSpy = vi.fn();
    window.addEventListener("harness-focus-tab", focusSpy as EventListener);
    window.addEventListener("harness-reviews-refresh", refreshSpy);

    render(
      <TranscriptList
        {...listProps([
          {
            kind: "pending_review",
            id: "rev-clickme01",
            summary: "Held 2 files for review",
          },
        ])}
      />,
    );

    fireEvent.click(screen.getByTestId("pending-review-receipt"));
    expect(focusSpy).toHaveBeenCalled();
    expect((focusSpy.mock.calls[0][0] as CustomEvent).detail).toBe("review");
    expect(refreshSpy).toHaveBeenCalled();

    window.removeEventListener("harness-focus-tab", focusSpy as EventListener);
    window.removeEventListener("harness-reviews-refresh", refreshSpy);
  });

  it("held_for_review SwarmResultCard click focuses Review tab and refreshes", () => {
    const focusSpy = vi.fn();
    const refreshSpy = vi.fn();
    window.addEventListener("harness-focus-tab", focusSpy as EventListener);
    window.addEventListener("harness-reviews-refresh", refreshSpy);

    render(
      <TranscriptList
        {...listProps([
          {
            kind: "swarm_result",
            job_id: "job_heldreentry01",
            applied: false,
            files: ["a.ts"],
            summary: "Patch held for review",
            error: null,
            objective: "ship patch",
            held_for_review: true,
          },
        ])}
      />,
    );

    const heldCard = screen.getByTestId("swarm-result-card");
    expect(heldCard).toHaveAttribute("data-outcome", "held");
    fireEvent.click(within(heldCard).getByRole("button", { name: /held for review/i }));

    expect(focusSpy).toHaveBeenCalled();
    expect((focusSpy.mock.calls[0][0] as CustomEvent).detail).toBe("review");
    expect(refreshSpy).toHaveBeenCalled();

    window.removeEventListener("harness-focus-tab", focusSpy as EventListener);
    window.removeEventListener("harness-reviews-refresh", refreshSpy);
  });
});

describe("investigation UX residual debts (nested / fold prefs / workerStream)", () => {
  afterEach(() => {
    clearActivityFoldPrefs();
  });

  it("shows nested worker actions when ActivityGroup is open without expanding the parent card", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "implement the fix" } },
      {
        kind: "card",
        card: {
          id: "run-impl-1",
          goal: "ship nested tools",
          cwd: null,
          kind: "run_implement",
          running: false,
          open: false,
          actions: [
            {
              action_id: "nested-read-1",
              kind: "read_file",
              goal: "webapp/src/App.tsx",
              status: "complete",
            },
            {
              action_id: "nested-edit-1",
              kind: "edit_file",
              goal: "webapp/src/App.tsx",
              status: "complete",
            },
          ],
          result: { status: "ok" },
        },
      },
      { kind: "msg", msg: { role: "assistant", text: "Done." } },
    ];

    render(<TranscriptList {...listProps(items)} />);
    // Kind buckets count nested rows (file + command + edit) while the fold
    // is closed — the lying-count bug was that those rows stayed invisible.
    const foldBtn = screen.getByRole("button", { name: /Explored/i });
    expect(foldBtn.textContent || "").toMatch(/1 file.*1 command.*1 edit/);
    // Nested rows must stay unmounted until the investigation fold opens.
    expect(screen.queryAllByTestId("nested-worker-action")).toHaveLength(0);

    fireEvent.click(foldBtn);
    // One expand level: opening Investigating reveals nested tools even though
    // the parent run_implement card stays open:false.
    const nested = screen.getAllByTestId("nested-worker-action");
    expect(nested).toHaveLength(2);
    expect(nested[0]).toHaveAttribute("data-action-id", "nested-read-1");
    expect(nested[1]).toHaveAttribute("data-action-id", "nested-edit-1");
  });

  it("marks failed nested worker rows with visible failed chrome", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "implement the fix" } },
      {
        kind: "card",
        card: {
          id: "run-impl-fail",
          goal: "ship nested tools",
          cwd: null,
          kind: "run_implement",
          running: false,
          open: false,
          actions: [
            {
              action_id: "nested-fail-1",
              kind: "read_file",
              goal: "missing.ts",
              status: "failed",
            },
          ],
          result: { status: "error", error: "worker failed" },
        },
      },
      { kind: "msg", msg: { role: "assistant", text: "Stopped." } },
    ];

    render(<TranscriptList {...listProps(items)} />);
    fireEvent.click(screen.getByRole("button", { name: /Explored/i }));
    expect(screen.getByLabelText("failed")).toBeTruthy();
    expect(screen.getByText("failed", { selector: ".sr-only" })).toBeTruthy();
  });

  it("clearActivityFoldPrefs drops sticky open state after a user toggle", () => {
    const card: Extract<Item, { kind: "card" }> = {
      kind: "card",
      card: {
        id: "fold-pref-card",
        goal: "auth.ts",
        cwd: null,
        kind: "read_file",
        running: false,
        open: false,
        result: { status: "ok" },
      },
    };
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "check auth" } },
      card,
    ];
    const groupId = activityGroupStableId([card], 0);

    render(<TranscriptList {...listProps(items)} />);
    fireEvent.click(screen.getByRole("button", { name: /Explored/i }));
    expect(resolveActivityGroupOpen(groupId)).toBe(true);

    clearActivityFoldPrefs();
    expect(resolveActivityGroupOpen(groupId)).toBe(false);
  });

  it("open fold renders absorbed workerStream via Bubble ticker, not muted pre", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "implement" } },
      {
        kind: "card",
        card: {
          id: "c-worker-preview",
          goal: "run implement",
          cwd: null,
          kind: "run_implement",
          running: true,
          open: false,
        },
      },
      {
        kind: "msg",
        msg: {
          role: "assistant",
          text: "worker live tokens line one\nworker live tokens line two",
          streaming: true,
          workerStream: true,
        },
      },
    ];

    const absorbed = collectIntermediateAssistantItems(items, true);
    expect(
      [...absorbed].some(
        (it) => it.kind === "msg" && it.msg.workerStream === true,
      ),
    ).toBe(true);

    render(
      <TranscriptList
        {...listProps(items)}
        status="awaiting_swarm"
        turnOpen
      />,
    );
    // Fold stays default-closed — do not force-open for workerStream.
    expect(screen.queryByText(/worker streaming/i)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Investigating/i }));
    // Bubble capped ticker chrome (label + tokens), not a muted narration <pre>.
    expect(screen.getByText(/worker streaming/i)).toBeTruthy();
    expect(screen.getByText(/worker live tokens line one/i)).toBeTruthy();
    const mutedPres = document.querySelectorAll("pre.text-muted\\/90");
    for (const pre of mutedPres) {
      expect(pre.textContent || "").not.toMatch(/worker live tokens/);
    }
  });
});

describe("job_id → Swarm Tracker deep-link chrome", () => {
  it("renders swarm_pending job ids as clickable chips that open the tracker", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    render(
      <TranscriptList
        {...listProps([
          {
            kind: "swarm_pending",
            job_ids: ["job_abcdef012345", "local-swarm-a1"],
            objective: "audit auth",
            status: "running",
          },
        ])}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Swarm|Investigating/i }));
    const chips = screen.getAllByTestId("swarm-pending-job-chip");
    expect(chips).toHaveLength(2);
    fireEvent.click(chips[0]!);
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).toContain("harness-focus-tab");
    expect(kinds).toContain("harness-open-swarm-job");
    const openEv = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-swarm-job");
    expect(openEv?.detail).toEqual({ jobId: "job_abcdef012345" });
    spy.mockRestore();
  });

  it("surfaces ActionCard spill peek CTA and opens harness-open-spill", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    render(
      <TranscriptList
        {...listProps([
          {
            kind: "card",
            card: {
              id: "spill-card",
              kind: "run_command",
              goal: "pytest -q",
              running: false,
              open: true,
              result: {
                command: "pytest -q",
                exit_code: 0,
                output: "…truncated…",
                spill_uri: "spill://sess1/call_a",
                output_spilled: true,
                output_chars: 9000,
              },
            },
          },
        ])}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Explored|Command|pytest/i }));
    const cta = screen.getByTestId("spill-output-peek");
    expect(cta).toHaveTextContent(/Full output \(9,?000 chars\)/);
    fireEvent.click(cta);
    expect(
      spy.mock.calls
        .map((c) => c[0] as CustomEvent)
        .some((e) => e.type === "harness-open-spill" && e.detail?.uri === "spill://sess1/call_a"),
    ).toBe(true);
    const spillKv = screen.getByTestId("spill-uri-link");
    expect(spillKv).toHaveTextContent("spill://sess1/call_a");
    spy.mockRestore();
  });

  it("makes ActionCard job KV and SwarmResultCard job ids open the tracker", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    render(
      <TranscriptList
        {...listProps([
          {
            kind: "card",
            card: {
              id: "c1",
              kind: "run_swarm",
              goal: "audit",
              running: false,
              open: true,
              result: { job_id: "job_abcdef012345", status: "pending" },
            },
          },
          {
            kind: "swarm_result",
            job_id: "job_deadbeef1234",
            applied: true,
            files: [],
            summary: "ok",
            error: null,
            objective: "audit",
            reuse_status: "reused",
            source_job_id: "local-bf1b30f4",
          },
        ])}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Explored/i }));

    const kvLink = screen.getByTestId("job-id-link");
    expect(kvLink).toHaveTextContent("job_abcdef012345");
    fireEvent.click(kvLink);
    expect(
      spy.mock.calls
        .map((c) => c[0] as CustomEvent)
        .some((e) => e.type === "harness-open-swarm-job" && e.detail?.jobId === "job_abcdef012345"),
    ).toBe(true);

    fireEvent.click(screen.getByText(/swarm done/i));
    const resultLinks = screen.getAllByTestId("swarm-result-job-link");
    expect(resultLinks.map((el) => el.textContent)).toEqual(
      expect.arrayContaining(["job_deadbeef1234", "local-bf1b30f4"]),
    );
    fireEvent.click(resultLinks.find((el) => el.textContent === "local-bf1b30f4")!);
    expect(
      spy.mock.calls
        .map((c) => c[0] as CustomEvent)
        .some((e) => e.type === "harness-open-swarm-job" && e.detail?.jobId === "local-bf1b30f4"),
    ).toBe(true);
    spy.mockRestore();
  });

  it("renders complete and partial machine-owned swarm delivery receipts", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const completeArtifacts = Array.from({ length: 17 }, (_, i) => ({
      id: `artifact-${i}`,
      task_id: `task-${i % 5}`,
      sha256: `sha-${i}`,
      type: i % 2 ? "risk" : "finding",
      headline: `Evidence ${i}`,
    }));
    const missing = [{ id: "artifact-16", task_id: "task-4" }];
    const persistedItems: Item[] = [
      {
        kind: "swarm_result",
        job_id: "job_111111111111",
        applied: true,
        files: [],
        summary: "17 findings via agentic (17 artifacts)",
        error: null,
        objective: "complete audit",
        cwd: "/repo/complete",
        reuse_status: "fresh",
        reuse_reason: "first_pass",
        artifacts: completeArtifacts,
        artifact_delivery: {
          pm_artifacts: 17,
          available_to_inspect: 17,
          complete: true,
          missing: [],
        },
      },
      {
        kind: "swarm_result",
        job_id: "job_222222222222",
        applied: true,
        files: [],
        summary: "16 findings via agentic (17 PM artifacts)",
        error: null,
        objective: "partial audit",
        cwd: "/repo/partial",
        reuse_status: "fresh",
        artifacts: completeArtifacts.slice(0, 16),
        artifact_delivery: {
          pm_artifacts: 17,
          available_to_inspect: 16,
          complete: false,
          missing,
        },
      },
    ];
    // Hydration/reload must retain the machine-owned receipt and warning.
    const hydratedItems = JSON.parse(JSON.stringify(persistedItems)) as Item[];

    render(<TranscriptList {...listProps(hydratedItems)} />);

    screen.getAllByText("swarm done").forEach((label) => {
      fireEvent.click(label.closest("button")!);
    });
    screen.getAllByTestId("swarm-delivery-toggle").forEach((toggle) => {
      expect(toggle).toHaveAttribute("aria-expanded", "false");
      fireEvent.click(toggle);
    });
    expect(screen.getAllByText("Artifacts").map((el) => el.parentElement?.textContent)).toEqual([
      expect.stringContaining("17"),
      expect.stringContaining("17"),
    ]);
    expect(screen.getByText("17/17")).toBeInTheDocument();
    expect(screen.getByText("16/17")).toBeInTheDocument();
    expect(screen.queryByText(/Delivered to Mari/i)).not.toBeInTheDocument();
    expect(screen.queryByText("fresh")).not.toBeInTheDocument();
    expect(screen.queryByText(/first_pass/)).not.toBeInTheDocument();
    expect(screen.getByText(/Synthesis continued with incomplete PM evidence/i)).toBeInTheDocument();
    expect(screen.getByText(/artifact-16.*task-4/i)).toBeInTheDocument();
    const artifactLinks = screen.getAllByTestId("swarm-artifact-link");
    expect(artifactLinks).toHaveLength(33);
    expect(screen.getAllByText("complete audit").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("/repo/complete")).toBeInTheDocument();
    fireEvent.click(artifactLinks[0]);
    const openEvent = spy.mock.calls
      .map((call) => call[0] as CustomEvent)
      .find((event) => event.type === "harness-open-swarm-job");
    expect(openEvent?.detail).toEqual({ jobId: "job_111111111111", artifactId: "artifact-0" });
    spy.mockRestore();
  });
});

describe("live command token clicks", () => {
  afterEach(() => {
    _resetAgentCommandIndexForTests();
  });

  it("clicks a registered `git pull` token into that agent terminal", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "pull latest" } },
          {
            kind: "card",
            card: {
              id: "card-git-pull",
              goal: "git pull",
              cwd: null,
              kind: "run_command",
              running: false,
              open: false,
              result: { status: "ok", command: "git pull", output: "Already up to date.\n" },
            },
          },
          {
            kind: "msg",
            msg: { role: "assistant", text: "Ran `git pull` and it was already up to date." },
          },
        ])}
      />,
    );

    const token = screen.getByRole("button", { name: "git pull" });
    expect(token).toHaveAttribute("title", "Reveal running command");
    fireEvent.click(token);

    const opened = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-agent-terminal");
    expect(opened?.detail).toEqual({
      id: "card-git-pull",
      command: "git pull",
      output: "Already up to date.\n",
    });
    expect(spy.mock.calls.map((c) => (c[0] as CustomEvent).type)).not.toContain("harness-run-command");
    spy.mockRestore();
  });

  it("leaves an unregistered command token as muted code", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "assistant", text: "You can run `git status` next." } },
        ])}
      />,
    );

    expect(screen.queryByRole("button", { name: "git status" })).toBeNull();
    const code = screen.getByText("git status");
    expect(code.tagName).toBe("CODE");
    expect(code).toHaveClass("text-txt/90");
  });

  it("renders dedicated trailing feed clearance after tail chrome", () => {
    render(
      <TranscriptList
        {...listProps([{ kind: "msg", msg: { role: "user", text: "hello" } }])}
      />,
    );

    const clearance = screen.getByTestId("feed-bottom-clearance");
    expect(clearance).toHaveAttribute("aria-hidden", "true");
    expect(clearance).toHaveClass("feed-bottom-clearance");
    expect(clearance.style.height).toBe("var(--feed-chrome-clearance, clamp(72px, 12vh, 144px))");
  });

  it("keeps a tool-free live answer outside the collapsed ActivityGroup", () => {
    const live: Item = {
      kind: "msg",
      msg: { role: "assistant", text: "I will patch auth next.", streaming: true },
    };
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "fix auth" } },
      live,
    ];
    expect(collectIntermediateAssistantItems(items, true).has(live)).toBe(false);
    render(
      <TranscriptList
        {...listProps(items)}
        status="streaming"
        turnOpen
      />,
    );
    expect(screen.getByText(/I will patch auth next/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Investigating|Explored/i })).toBeNull();
  });

  it("groups consecutive read/search cards into one exploration shelf", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "look around" } },
          {
            kind: "card",
            card: {
              id: "r1",
              goal: "auth.ts",
              cwd: null,
              kind: "read_file",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
          {
            kind: "card",
            card: {
              id: "g1",
              goal: "login",
              cwd: null,
              kind: "grep",
              running: false,
              open: false,
              result: { status: "ok" },
            },
          },
        ])}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Explored/i }));
    expect(screen.getByTestId("exploration-shelf")).toBeTruthy();
    expect(screen.getByTestId("exploration-shelf")).toHaveAttribute("data-count", "2");
  });

  it("does not remount the exploration shelf when a consecutive card appends", () => {
    const card = (id: string, running = false): Item => ({
      kind: "card",
      card: {
        id,
        goal: `${id}.ts`,
        cwd: null,
        kind: "read_file",
        running,
        open: false,
        result: running ? undefined : { status: "ok" },
      },
    });
    const user: Item = { kind: "msg", msg: { role: "user", text: "look around" } };
    const { rerender } = render(
      <TranscriptList
        {...listProps([user, card("r1", true), card("g1", true)])}
        status="executing"
        turnOpen
      />,
    );
    if (!screen.queryByRole("button", { name: /^Exploration /i })) {
      fireEvent.click(screen.getByRole("button", { name: /Investigating|Explored/i }));
    }
    const shelfToggle = screen.getByRole("button", { name: /^Exploration /i });
    expect(shelfToggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(shelfToggle);
    expect(shelfToggle).toHaveAttribute("aria-expanded", "false");
    rerender(
      <TranscriptList
        {...listProps([user, card("r1", true), card("g1", true), card("r2", true)])}
        status="executing"
        turnOpen
      />,
    );
    const shelfAfter = screen.getByRole("button", { name: /^Exploration /i });
    expect(screen.getByTestId("exploration-shelf")).toHaveAttribute("data-count", "3");
    expect(shelfAfter).toHaveAttribute("aria-expanded", "false");
  });
});
