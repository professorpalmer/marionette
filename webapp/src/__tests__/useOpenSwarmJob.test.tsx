import { act, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const opened = vi.hoisted(() => [] as unknown[]);
vi.mock("../lib/agentLinks", async (orig) => ({
  ...(await orig<typeof import("../lib/agentLinks")>()),
  openAgentSwarmJob: (target: unknown) => { opened.push(target); },
}));

import { JobMetadataContext } from "../lib/jobMetadataContext";
import { JobMetadataStore } from "../lib/useJobMetadata";
import { useOpenSwarmJob } from "../lib/useOpenSwarmJob";

afterEach(() => { opened.length = 0; });

it("does not re-render its host on metadata ticks, yet opens with the latest snapshot", () => {
  const store = new JobMetadataStore();
  store.setTarget({ repo: "/r/a", session_id: "s1", scope: "all" });
  let renders = 0;
  let open: ((jobId: string) => void) | undefined;
  function Host() {
    renders += 1;
    open = useOpenSwarmJob("s2");
    return null;
  }
  render(<JobMetadataContext.Provider value={store}><Host /></JobMetadataContext.Provider>);
  const before = renders;
  act(() => {
    store.setTarget({ repo: "/r/b", session_id: "s1", scope: "all" });
    store.setTarget({ repo: "/r/c", session_id: "s2", scope: "all" });
  });
  expect(renders).toBe(before);
  open!("job_abc123def456");
  expect(opened).toHaveLength(1);
  expect(opened[0]).toMatchObject({ jobId: "job_abc123def456", context: { repo: "/r/c", session_id: "s2" } });
  store.dispose();
});
