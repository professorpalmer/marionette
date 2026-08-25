/**
 * Session-list title lock helpers.
 *
 * Investigating / Explored / Diagnosing / Planning walls and bare Stopped.
 * belong in the activity strip — never session.title or preferred list preview.
 */

/** Activity / progress headlines that must never become session.title. */
export function isActivityHeadlineText(text: string): boolean {
  const raw = String(text || "").trim();
  if (!raw) return false;
  if (/^Stopped\.?$/i.test(raw)) return true;

  const lineRe =
    /^(Investigating|Explored|Diagnosing|Inspecting|Planning|Assessing|Still working|Looking|Thinking|Thought|Worked for|Ran\s+\d+\s+commands?|Working\.\.\.?)\b/i;

  const lines = raw
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length === 0) return false;
  if (lines.length === 1) return lineRe.test(lines[0]!);

  // Wall of investigating / Explored / Diagnosing lines from a long turn.
  const hits = lines.filter((l) => lineRe.test(l)).length;
  return hits >= 2 || (hits === 1 && hits === lines.length);
}
