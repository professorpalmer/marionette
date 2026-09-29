import { expect, it } from "vitest";
import { DIFF_RENDER_LINE_CAP, capDiffHunks, parseDiffHunks } from "../components/SourceControl";

function diffWith(bodyLines: number, hunks = 1): string {
  const out = ["diff --git a/x b/x", "--- a/x", "+++ b/x"];
  for (let h = 0; h < hunks; h += 1) {
    out.push(`@@ -${h * 10},1 +${h * 10},1 @@`);
    for (let i = 0; i < bodyLines; i += 1) out.push(`+line ${h}.${i}`);
  }
  return out.join("\n");
}

it("parses header, hunks and body line totals", () => {
  const parsed = parseDiffHunks(diffWith(3, 2));
  expect(parsed.hasHunks).toBe(true);
  expect(parsed.headerLines).toHaveLength(3);
  expect(parsed.hunks.map((h) => h.lines.length)).toEqual([4, 4]);
  expect(parsed.totalLines).toBe(6);
});

it("caps painted lines across hunks and truncates the last one to fit", () => {
  const parsed = parseDiffHunks(diffWith(2000, 3));
  const capped = capDiffHunks(parsed.hunks, DIFF_RENDER_LINE_CAP);
  expect(capped.reduce((n, h) => n + h.lines.length - 1, 0)).toBe(DIFF_RENDER_LINE_CAP);
  expect(capped).toHaveLength(2);
  expect(capped[1].lines[0]).toBe(parsed.hunks[1].header);
});

it("leaves a small diff untouched", () => {
  const parsed = parseDiffHunks(diffWith(5, 2));
  expect(capDiffHunks(parsed.hunks, DIFF_RENDER_LINE_CAP)).toEqual(parsed.hunks);
});
