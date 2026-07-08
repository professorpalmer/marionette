// Renderer-side guard for update progress text.
//
// The update pipeline's progress messages are produced by the Electron MAIN
// process, which in a packaged install runs from the Setup.exe bundle (ASAR) --
// not from the git checkout this renderer was built from. An older bundled
// updater streams raw child-process output (npm deprecation warnings, git
// chatter, pip noise) as progress messages, and those read like errors when
// they scroll across the banner and status pill. The checkout-side updater was
// fixed to send calm stage labels, but the shell only picks that up on a
// reinstall -- so the renderer sanitizes too, covering every shell version.

const CALM_STAGE_LABELS: Record<string, string> = {
  fetch: "Fetching latest changes",
  pull: "Updating source",
  deps: "Updating dependencies",
  build: "Rebuilding app",
  done: "Update ready -- relaunching",
  prepare: "Preparing update",
};

// Raw tool output that must never render as a user-facing progress message.
const RAW_TOOL_OUTPUT = /^(npm (warn|error|notice|WARN|ERR!)|warning:|remote:|Receiving objects|Resolving deltas|Compressing objects|Counting objects|audited \d|added \d+ packages|up to date|found \d+ vulnerabilit|Collecting |Downloading |Installing collected|Requirement already|Successfully installed|Building wheel|Preparing metadata|deprecated )/i;

// A calm label is short and single-line; anything long or multi-line is raw
// transcript spill even if it dodged the prefix patterns.
const MAX_LABEL_LENGTH = 120;

export function looksLikeRawToolOutput(message: string): boolean {
  if (!message) return false;
  if (message.includes("\n")) return true;
  if (message.length > MAX_LABEL_LENGTH) return true;
  return RAW_TOOL_OUTPUT.test(message.trim());
}

// Returns a message safe to show in the banner/pill: the original when it is
// already a calm label, otherwise a stage-appropriate fallback.
export function sanitizeUpdateMessage(stage: string, message: string): string {
  const fallback = CALM_STAGE_LABELS[stage] || "Updating";
  if (!message) return fallback;
  return looksLikeRawToolOutput(message) ? fallback : message;
}
