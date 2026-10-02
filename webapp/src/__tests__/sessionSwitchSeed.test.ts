import { describe, expect, it } from "vitest";
import { busySeedOnSwitch } from "../components/conversation/sessionHydrate";

describe("busy chrome seed on session switch", () => {
  it("shows Stop at once for a target the feed reports running", () => {
    // Defaulting to idle + pending painted Stop -> Send -> Stop on every
    // switch between two running sessions.
    expect(busySeedOnSwitch("running")).toEqual({ pending: false, turnOpen: true, status: "thinking" });
  });

  it("holds Send pending for idle, attaching, missing or unknown targets", () => {
    for (const runner of ["idle", "attaching", "missing", undefined]) {
      expect(busySeedOnSwitch(runner)).toEqual({ pending: true, turnOpen: false, status: "idle" });
    }
  });
});
