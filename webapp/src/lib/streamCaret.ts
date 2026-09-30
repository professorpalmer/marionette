import type { Element, ElementContent, Root, RootContent } from "hast";

export const STREAM_CARET_CLASS = "transcript-stream-caret";

export function streamCaretElement(): Element {
  return {
    type: "element",
    tagName: "span",
    properties: { className: [STREAM_CARET_CLASS], ariaHidden: "true" },
    children: [],
  };
}

function isBlank(node: RootContent | ElementContent): boolean {
  return node.type === "text" ? !node.value.trim() : node.type === "comment" || node.type === "doctype";
}

/**
 * Rehype plugin: put the streaming caret after the last visible text, inside
 * its element. A caret placed beside a block gets a line box of its own, and
 * that line vanishes when streaming ends, jumping everything below it.
 */
export function rehypeStreamCaret() {
  return (tree: Root) => {
    let parent: Root | Element = tree;
    for (;;) {
      const children: (RootContent | ElementContent)[] = parent.children;
      let i = children.length - 1;
      while (i >= 0 && isBlank(children[i]!)) i -= 1;
      const last = i >= 0 ? children[i]! : null;
      if (last?.type === "element" && last.children.some((c) => !isBlank(c))) {
        parent = last;
        continue;
      }
      children.splice(i + 1, 0, streamCaretElement());
      return;
    }
  };
}
