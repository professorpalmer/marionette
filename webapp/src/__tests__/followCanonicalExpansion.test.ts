import { expect, it } from "vitest";
import { followCanonicalExpansion, localKey } from "../lib/localJobMetadata";

const alias = localKey({ job_id: "local-swarm-call", incarnation: "inc-1" });
const pmKey = JSON.stringify(["harness", "state-A", "job_canonical", "s", "/repo", 2, "pm-inc"]);

it("moves an expanded local alias to the PM row that replaced it", () => {
  const jobs = [{ metadata_key: pmKey, canonical_aliases: ["local-swarm-call"] }];
  expect(followCanonicalExpansion([alias], jobs)).toEqual([pmKey]);
  expect(followCanonicalExpansion(["other", alias, pmKey], jobs)).toEqual(["other", pmKey]);
});

it("leaves expansion alone when no PM row claims the alias", () => {
  const expanded = [alias, "not json"];
  expect(followCanonicalExpansion(expanded, [{ metadata_key: pmKey, canonical_aliases: [] }])).toBe(expanded);
  expect(followCanonicalExpansion(expanded, [{ metadata_key: pmKey }])).toBe(expanded);
});
