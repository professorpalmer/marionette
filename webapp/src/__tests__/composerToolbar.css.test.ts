import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";

const css = readFileSync(resolve(__dirname, "../index.css"), "utf8");

it("keeps the composer picker content-sized so Queue/Interrupt/Steer have a lane", () => {
  expect(css).toMatch(/\.composer-toolbar \{[^}]*flex-wrap:\s*nowrap;/);
  expect(css).toMatch(/\.composer-toolbar-actions \{[^}]*flex:\s*1 1 auto;/);
  expect(css).toMatch(/\.composer-toolbar-send \{[^}]*flex:\s*0 0 auto;/);
  expect(css).toMatch(/\.pilot-picker-slot \{[^}]*flex:\s*0 1 auto;/);
  expect(css).not.toMatch(/\.pilot-picker-slot \{[^}]*flex:\s*1 1 /);
});
