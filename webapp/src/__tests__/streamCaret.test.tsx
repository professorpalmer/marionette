import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Markdown } from "../components/TranscriptList";

function caretOf(container: HTMLElement): HTMLElement {
  const carets = container.querySelectorAll(".transcript-stream-caret");
  expect(carets).toHaveLength(1);
  return carets[0] as HTMLElement;
}

// A caret beside a block element gets a line box of its own, so the bubble
// shrinks a line when streaming stops and everything below it jumps.
describe("streaming caret", () => {
  it("sits inside the last paragraph, not after it", () => {
    const { container } = render(<Markdown streaming text={"First paragraph.\n\nNow let me run probe 1."} />);
    const caret = caretOf(container);
    expect(caret.parentElement?.tagName).toBe("P");
    expect(caret.parentElement?.textContent).toContain("Now let me run probe 1.");
  });

  it("sits inside the last list item", () => {
    const { container } = render(<Markdown streaming text={"Steps:\n\n- one\n- two"} />);
    expect(caretOf(container).closest("li")?.textContent).toContain("two");
  });

  it("sits inside an open code fence", () => {
    const { container } = render(<Markdown streaming text={"Run:\n```bash\necho hi\n"} />);
    expect(caretOf(container).parentElement?.tagName).toBe("PRE");
  });

  it("is gone once streaming ends", () => {
    const { container } = render(<Markdown text={"Done."} />);
    expect(container.querySelector(".transcript-stream-caret")).toBeNull();
  });
});

describe("sealing a streamed answer", () => {
  it("keeps the rendered blocks mounted; only the caret goes away", () => {
    const text = "First paragraph.\n\n```ts\nconst a = 1;\n```\n\nLast line.";
    const { container, rerender } = render(<Markdown streaming text={text} />);
    const firstP = container.querySelector("p");
    const code = container.querySelector("code");
    rerender(<Markdown text={text} />);
    expect(container.querySelector("p")).toBe(firstP);
    expect(container.querySelector("code")).toBe(code);
    expect(container.querySelector(".transcript-stream-caret")).toBeNull();
  });
});
