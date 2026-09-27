import { describe, expect, it } from "vitest";
import { parseSamplingDraft } from "../lib/localSampling";

const baseDraft = {
  temperature: "",
  top_p: "",
  frequency_penalty: "",
};

describe("parseSamplingDraft", () => {
  it.each(["-1", "0", "128", "262144"])("preserves integer reasoning budget %s", (value) => {
    expect(parseSamplingDraft({ ...baseDraft, reasoning_budget_tokens: value })).toEqual({
      ok: true,
      sampling: { reasoning_budget_tokens: Number(value) },
    });
  });

  it.each([true, 1.5, "1.5", "NaN", "Infinity", "-2", "262145"])(
    "rejects invalid reasoning budget %s",
    (value) => {
      const result = parseSamplingDraft({ ...baseDraft, reasoning_budget_tokens: value });
      expect(result.ok).toBe(false);
    },
  );

  it("omits a blank reasoning budget", () => {
    expect(parseSamplingDraft({ ...baseDraft, reasoning_budget_tokens: "" })).toEqual({
      ok: true,
      sampling: {},
    });
  });
});
