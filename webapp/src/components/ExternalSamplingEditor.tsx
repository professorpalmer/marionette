import { useState } from "react";
import type { LocalExternalEndpoint, LocalModelCommand } from "../lib/api";
import { SAMPLING_FIELDS, draftFromSampling, parseSamplingDraft, type SamplingDraft } from "../lib/localSampling";

const INPUT_CLASS = "w-20 px-2 py-1 rounded-md bg-panel2 border border-edge/50 text-ui-11 text-txt outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent";
const BUTTON_CLASS = "px-2 py-1 rounded-md border border-edge/40 text-ui-11 text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent";

export default function ExternalSamplingEditor({
  endpoint,
  disabled,
  onCommand,
  onError,
}: {
  endpoint: LocalExternalEndpoint;
  disabled: boolean;
  onCommand: (command: LocalModelCommand) => void;
  onError: (message: string) => void;
}) {
  const label = endpoint.name || endpoint.id;
  const [model, setModel] = useState(endpoint.selected_model || endpoint.models[0] || "");
  const [drafts, setDrafts] = useState<Record<string, SamplingDraft>>({});
  const draft = drafts[model] ?? draftFromSampling(endpoint.sampling?.[model]);
  if (!model) return null;

  const setField = (key: keyof SamplingDraft, value: string) =>
    setDrafts((prev) => ({ ...prev, [model]: { ...draft, [key]: value } }));

  const save = (sampling: SamplingDraft) => {
    const parsed = parseSamplingDraft(sampling);
    if (!parsed.ok) {
      onError(parsed.error);
      return;
    }
    onCommand({ type: "set_sampling", endpoint_id: endpoint.id, model, sampling: parsed.sampling });
  };

  return (
    <div className="mt-2" data-testid={`local-external-sampling-${endpoint.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-ui-11 text-muted">Sampling</span>
        {endpoint.models.length > 1 ? (
          <select
            aria-label={`Sampling model for ${label}`}
            value={model}
            onChange={(event) => setModel(event.target.value)}
            className="px-2 py-1 rounded-md bg-panel2 border border-edge/50 text-ui-11 text-txt"
          >
            {endpoint.models.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        ) : (
          <span className="text-ui-11 text-txt">{model}</span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 mt-1">
        {SAMPLING_FIELDS.map((field) => (
          <label key={field.key} className="flex items-center gap-1 text-ui-11 text-muted">
            {field.label}
            <input
              type="number"
              min={field.min}
              max={field.max}
              step={field.step}
              aria-label={`${field.label} for ${label} ${model}`}
              value={draft[field.key]}
              placeholder="default"
              onChange={(event) => setField(field.key, event.target.value)}
              className={INPUT_CLASS}
            />
          </label>
        ))}
        <button type="button" className={BUTTON_CLASS} disabled={disabled} onClick={() => save(draft)}>
          Save sampling
        </button>
        <button
          type="button"
          className={BUTTON_CLASS}
          disabled={disabled}
          onClick={() => {
            const cleared = draftFromSampling(undefined);
            setDrafts((prev) => ({ ...prev, [model]: cleared }));
            save(cleared);
          }}
        >
          Use server defaults
        </button>
      </div>
      <p className="mt-1 text-ui-11 text-muted">
        Reasoning budget: blank uses the server default; -1 follows server semantics and may inherit its default;
        0 requests an immediate end to thinking when supported; positive values request a reasoning token budget.
        Requires endpoint support (for example, llama.cpp/Bonsai); it does not limit visible answer tokens and is not a
        universal provider setting.
      </p>
    </div>
  );
}
