import { act, render } from "@testing-library/react";
import { expect, it } from "vitest";
import { JobMetadataContext, useSharedJobMetadataFields } from "../lib/jobMetadataContext";
import { JobMetadataStore } from "../lib/useJobMetadata";

const FIELDS = ["observations", "view"] as const;

it("a publish that changes no subscribed field does not re-render", () => {
  const store = new JobMetadataStore();
  let renders = 0;
  function Probe() {
    useSharedJobMetadataFields(FIELDS);
    renders += 1;
    return null;
  }
  render(<JobMetadataContext.Provider value={store}><Probe /></JobMetadataContext.Provider>);
  const start = renders;
  const publish = (next: object) => (store as unknown as { publish(s: object): void }).publish(next);
  act(() => {
    for (let i = 0; i < 5; i += 1) {
      const s = store.getSnapshot();
      publish({ ...s, advanceNumber: s.advanceNumber + 1, working: !s.working });
    }
  });
  expect(renders).toBe(start);
  act(() => { publish({ ...store.getSnapshot(), observations: [] }); });
  expect(renders).toBe(start + 1);
});
