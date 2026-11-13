import { describe, expect, it } from "vitest";
import { projectName } from "../components/conversation/useSessionHeading";

describe("projectName", () => {
  it.each([
    ["/Users/a/Projects/marionette", "marionette"],
    ["/Users/a/Projects/marionette/", "marionette"],
    ["C:\\Users\\a\\proj", "proj"],
    ["", ""],
  ])("%s -> %s", (repo, name) => {
    expect(projectName(repo)).toBe(name);
  });
});
