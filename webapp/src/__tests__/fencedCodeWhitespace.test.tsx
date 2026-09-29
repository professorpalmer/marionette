import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TranscriptList, type Item } from "../components/TranscriptList";

afterEach(() => cleanup());

it("fenced code keeps its line breaks", () => {
  // Plain fenced code rendered as a bare <code> without whitespace-pre, so
  // multi-line snippets in answers collapsed onto one line.
  const items: Item[] = [
    { kind: "msg", msg: { role: "user", text: "show code" } },
    { kind: "msg", msg: { role: "assistant", text: "```python\nimport sys\nsys.exit(7)\n```" } },
  ];
  const { container } = render(
    <TranscriptList
      items={items}
      status="idle"
      compactingStatus={null}
      editingIndex={null}
      auto={false}
      plan={false}
      scrollContainerRef={{ current: null }}
      onEditMessage={vi.fn()}
      onExecuteSend={vi.fn()}
      onImageClick={vi.fn()}
      onSetCard={vi.fn()}
      onExecutePlan={vi.fn()}
      onCommandApproval={vi.fn()}
    />,
  );
  const code = [...container.querySelectorAll("code")].find(c => c.textContent?.includes("sys.exit(7)"));
  expect(code?.textContent).toContain("import sys\nsys.exit(7)");
  expect(code?.className.split(/\s+/)).toContain("whitespace-pre");
});
