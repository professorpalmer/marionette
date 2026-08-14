import { describe, expect, it } from "vitest";
import {
  canOpenLeftColumn,
  defaultColumns,
  extractCardToLeftColumn,
  flattenColumns,
  moveCardIntoColumn,
  reconcileColumns,
} from "../lib/boardColumns";

describe("boardColumns", () => {
  it("defaults every open card into one rightmost column", () => {
    expect(defaultColumns(["review", "swarm", "browser"])).toEqual([
      ["review", "swarm", "browser"],
    ]);
    expect(defaultColumns([])).toEqual([]);
  });

  it("reconciles saved columns with the live open-card list", () => {
    expect(reconcileColumns(
      ["review", "swarm", "browser"],
      [["review", "gone"], ["browser"]],
    )).toEqual([["review", "swarm"], ["browser"]]);
  });

  it("inserts a card into another column and drops empty stacks", () => {
    const next = moveCardIntoColumn(
      [["review", "swarm"], ["browser"]],
      "browser",
      0,
      1,
    );
    expect(next).toEqual([["review", "browser", "swarm"]]);
    expect(flattenColumns(next)).toEqual(["review", "browser", "swarm"]);
  });

  it("extracts a stacked card into a new leftmost column", () => {
    expect(extractCardToLeftColumn(
      [["review", "swarm", "browser"]],
      "browser",
    )).toEqual([["review", "swarm"], ["browser"]]);
    expect(canOpenLeftColumn([["review", "swarm", "browser"]], "browser")).toBe(true);
    expect(canOpenLeftColumn([["review"], ["browser"]], "browser")).toBe(false);
  });
});
