import { expect, it } from "vitest";
import { formatCompactErrorMessage, formatSteerErrorMessage, formatInterruptErrorMessage, formatRenderCommandErrorMessage } from "../components/conversation/composerSend";
it.each([new Error("SECRET transport body"), { message: { secret: true } }, "SECRET server text", null])("sanitizes unexpected composer failures %s", error => {
  for (const format of [formatCompactErrorMessage, formatSteerErrorMessage, formatInterruptErrorMessage, formatRenderCommandErrorMessage]) {
    expect(format(error)).not.toMatch(/SECRET|object Object|secret/);
    expect(format(error)).toMatch(/try again/i);
  }
});
it("preserves known input recovery copy", () => {
  expect(formatSteerErrorMessage({ body: { code: "input_stopped" } })).toContain("Stop cancelled this input");
});
