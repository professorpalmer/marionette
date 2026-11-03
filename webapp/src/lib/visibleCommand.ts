// Display copy of a command that a person must approve. The copy shows
// everything that will run: a hidden code point (zero-width, bidi override,
// tag character, variation selector, control or escape byte) becomes a
// visible \u{...} escape, so a command cannot look shorter or different
// from what runs. Long space and blank-line runs collapse to a marker, so
// padding cannot push the end of a command out of the scroll box.
// The executed command and the approval identity never use this copy.

const SPACE_RUN = 8;
const BLANK_LINE_RUN = 3;

function isHidden(cp: number): boolean {
  if (cp < 0x20) return cp !== 0x0a && cp !== 0x09; // C0 except LF and TAB (CR, ESC shown)
  if (cp === 0x7f || (cp >= 0x80 && cp <= 0x9f)) return true; // DEL, C1
  if (cp === 0x00ad || cp === 0x034f || cp === 0x061c || cp === 0x180e) return true;
  if (cp >= 0x200b && cp <= 0x200f) return true; // zero-width, LRM, RLM
  if (cp >= 0x2028 && cp <= 0x202e) return true; // line/para separators, bidi embed/override
  if (cp >= 0x2060 && cp <= 0x206f) return true; // word joiner, invisible operators, isolates
  if (cp === 0xfeff) return true; // BOM / zero-width no-break space
  if (cp >= 0xfe00 && cp <= 0xfe0f) return true; // variation selectors
  if (cp >= 0xfff9 && cp <= 0xfffb) return true; // interlinear annotation
  if (cp >= 0xe0000 && cp <= 0xe007f) return true; // tag block
  if (cp >= 0xe0100 && cp <= 0xe01ef) return true; // variation selectors supplement
  return false;
}

function escapeCodePoint(cp: number): string {
  return `\\u{${cp.toString(16).toUpperCase().padStart(4, "0")}}`;
}

export function visibleCommand(command: string): string {
  let out = "";
  for (const ch of command) {
    const cp = ch.codePointAt(0) ?? 0;
    out += isHidden(cp) ? escapeCodePoint(cp) : ch;
  }
  out = out.replace(new RegExp(`[ \\t]{${SPACE_RUN},}`, "g"), (run) => ` [${run.length} spaces] `);
  out = out.replace(
    new RegExp(`\\n{${BLANK_LINE_RUN + 1},}`, "g"),
    (run) => `\n[${run.length - 1} blank lines]\n`,
  );
  return out;
}
