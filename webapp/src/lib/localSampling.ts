import type { LocalSampling } from "./api";

export type SamplingField = keyof LocalSampling;

/** Mirrors harness/local_models.py SAMPLING_BOUNDS. */
export const SAMPLING_FIELDS: ReadonlyArray<{
  key: SamplingField;
  label: string;
  min: number;
  max: number;
  minInclusive: boolean;
  step: number;
  integerOnly?: boolean;
}> = [
  { key: "temperature", label: "Temperature", min: 0, max: 2, minInclusive: true, step: 0.05 },
  { key: "top_p", label: "Top P", min: 0, max: 1, minInclusive: false, step: 0.01 },
  { key: "frequency_penalty", label: "Frequency penalty", min: -2, max: 2, minInclusive: true, step: 0.05 },
  {
    key: "reasoning_budget_tokens",
    label: "Reasoning budget",
    min: -1,
    max: 262144,
    minInclusive: true,
    step: 1,
    integerOnly: true,
  },
];

export type SamplingDraft = Record<SamplingField, string>;

export function draftFromSampling(sampling: LocalSampling | undefined): SamplingDraft {
  const draft: SamplingDraft = {
    temperature: "",
    top_p: "",
    frequency_penalty: "",
    reasoning_budget_tokens: "",
  };
  for (const field of SAMPLING_FIELDS) {
    const value = sampling?.[field.key];
    if (typeof value === "number") draft[field.key] = String(value);
  }
  return draft;
}

/** Blank fields are omitted so the server default applies. */
export function parseSamplingDraft(
  draft: Record<SamplingField, unknown>,
): { ok: true; sampling: LocalSampling } | { ok: false; error: string } {
  const sampling: LocalSampling = {};
  for (const field of SAMPLING_FIELDS) {
    const raw = draft[field.key];
    if (typeof raw !== "string") {
      return { ok: false, error: `${field.label} must be a number` };
    }
    const text = raw.trim();
    if (!text) continue;
    const value = Number(text);
    const belowMin = field.minInclusive ? value < field.min : value <= field.min;
    if (!Number.isFinite(value) || (field.integerOnly && !Number.isInteger(value)) || belowMin || value > field.max) {
      if (field.integerOnly) {
        return { ok: false, error: `${field.label} must be an integer from ${field.min} to ${field.max}` };
      }
      const lower = field.minInclusive ? `${field.min} to` : `above ${field.min} up to`;
      return { ok: false, error: `${field.label} must be ${lower} ${field.max}` };
    }
    sampling[field.key] = value;
  }
  return { ok: true, sampling };
}
