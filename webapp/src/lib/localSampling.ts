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
}> = [
  { key: "temperature", label: "Temperature", min: 0, max: 2, minInclusive: true, step: 0.05 },
  { key: "top_p", label: "Top P", min: 0, max: 1, minInclusive: false, step: 0.01 },
  { key: "frequency_penalty", label: "Frequency penalty", min: -2, max: 2, minInclusive: true, step: 0.05 },
];

export type SamplingDraft = Record<SamplingField, string>;

export function draftFromSampling(sampling: LocalSampling | undefined): SamplingDraft {
  const draft = { temperature: "", top_p: "", frequency_penalty: "" };
  for (const field of SAMPLING_FIELDS) {
    const value = sampling?.[field.key];
    if (typeof value === "number") draft[field.key] = String(value);
  }
  return draft;
}

/** Blank fields are omitted so the server default applies. */
export function parseSamplingDraft(
  draft: SamplingDraft,
): { ok: true; sampling: LocalSampling } | { ok: false; error: string } {
  const sampling: LocalSampling = {};
  for (const field of SAMPLING_FIELDS) {
    const text = draft[field.key].trim();
    if (!text) continue;
    const value = Number(text);
    const belowMin = field.minInclusive ? value < field.min : value <= field.min;
    if (!Number.isFinite(value) || belowMin || value > field.max) {
      const lower = field.minInclusive ? `${field.min} to` : `above ${field.min} up to`;
      return { ok: false, error: `${field.label} must be ${lower} ${field.max}` };
    }
    sampling[field.key] = value;
  }
  return { ok: true, sampling };
}
