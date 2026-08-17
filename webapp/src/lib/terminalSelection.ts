/**
 * Hermes-style "Add to chat" helpers for the built-in xterm pane.
 * Steal the behavior (selection mention + Cmd/Ctrl+L), not the chrome.
 */

export const ADD_TERMINAL_SELECTION_EVENT = "harness-add-terminal-selection";

export type TerminalSelectionDetail = {
  text: string;
  label: string;
};

export type TerminalSelectionPosition = {
  start: { y: number };
  end: { y: number };
};

/** `@terminal:zsh:12` or `@terminal:"spaced label"`. */
export const TERMINAL_REF_RE = /@terminal:(?:"([^"]+)"|(\S+))/g;

export function isMacNavigator(
  nav: { platform?: string; userAgent?: string } = typeof navigator !== "undefined"
    ? navigator
    : {},
): boolean {
  const platform = String(nav.platform || "");
  if (platform) return /mac/i.test(platform);
  return /mac/i.test(String(nav.userAgent || ""));
}

export function isAddSelectionShortcut(
  event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "shiftKey">,
  isMac: boolean,
): boolean {
  const mod = isMac ? event.metaKey : event.ctrlKey;
  return Boolean(mod && !event.shiftKey && event.key.toLowerCase() === "l");
}

export function addToChatShortcutHint(isMac: boolean): string {
  return isMac ? "Cmd+L" : "Ctrl+L";
}

export function terminalSelectionLabel(
  text: string,
  shellName: string,
  position?: TerminalSelectionPosition | null,
): string {
  const name = String(shellName || "term").trim() || "term";
  if (position) {
    return position.start.y === position.end.y
      ? `${name}:${position.start.y}`
      : `${name}:${position.start.y}-${position.end.y}`;
  }
  const lines = Math.max(1, text.replace(/\s+$/g, "").split(/\r?\n/).length);
  return lines === 1 ? `${name}:1` : `${name}:1-${lines}`;
}

type SelectionHost = {
  clientWidth: number;
  clientHeight: number;
  getBoundingClientRect: () => { left: number; top: number };
  querySelectorAll: (selector: string) => ArrayLike<{ getBoundingClientRect: () => { width: number; height: number; left: number; top: number; bottom: number } }>;
};

export function terminalSelectionAnchor(
  host: SelectionHost | null,
  buttonWidth = 120,
): { left: number; top: number } | null {
  if (!host) return null;
  const rects = Array.from(host.querySelectorAll(".xterm-selection div"))
    .map((node) => node.getBoundingClientRect())
    .filter((r) => r.width > 0 && r.height > 0);
  const rect = rects[rects.length - 1];
  if (!rect) return null;
  const hostRect = host.getBoundingClientRect();
  const left = Math.min(
    Math.max(rect.left - hostRect.left, 8),
    Math.max(8, host.clientWidth - buttonWidth - 8),
  );
  const top = Math.min(
    Math.max(rect.bottom - hostRect.top + 4, 8),
    Math.max(8, host.clientHeight - 34),
  );
  return { left, top };
}

export function formatTerminalMention(label: string): string {
  const trimmed = String(label || "").trim();
  if (!trimmed) return "";
  if (/\s/.test(trimmed)) {
    return `@terminal:"${trimmed.replace(/"/g, "")}"`;
  }
  return `@terminal:${trimmed}`;
}

export function appendTerminalMention(draft: string, label: string): string {
  const token = formatTerminalMention(label);
  if (!token) return draft;
  if (draft.includes(token)) return draft;
  const trimmed = draft.replace(/\s+$/g, "");
  if (!trimmed) return `${token} `;
  return `${trimmed} ${token} `;
}

export function terminalLabelsFromDraft(draft: string): string[] {
  const labels: string[] = [];
  const re = new RegExp(TERMINAL_REF_RE.source, "g");
  let match: RegExpExecArray | null = re.exec(draft);
  while (match) {
    const label = match[1] || match[2] || "";
    if (label) labels.push(label);
    match = re.exec(draft);
  }
  return labels;
}

export function applyTerminalSelectionsToMessage(
  message: string,
  selections: Record<string, string>,
): string {
  return message.replace(TERMINAL_REF_RE, (full, quoted: string, bare: string) => {
    const label = quoted || bare || "";
    const body = selections[label];
    if (!body) return full;
    return "```terminal\n" + body.replace(/\s+$/g, "") + "\n```";
  });
}

export function dispatchAddTerminalSelection(text: string, label: string): void {
  const trimmed = String(text || "").trim();
  const tag = String(label || "").trim();
  if (!trimmed || !tag) return;
  window.dispatchEvent(
    new CustomEvent<TerminalSelectionDetail>(ADD_TERMINAL_SELECTION_EVENT, {
      detail: { text: trimmed, label: tag },
    }),
  );
}

export function readLiveTerminalSelection(term: { getSelection?: () => string } | null): string {
  if (!term || typeof term.getSelection !== "function") return "";
  try {
    return String(term.getSelection() || "");
  } catch {
    return "";
  }
}

export function readTerminalSelectionPosition(
  term: { getSelectionPosition?: () => TerminalSelectionPosition | undefined | null } | null,
): TerminalSelectionPosition | null {
  if (!term || typeof term.getSelectionPosition !== "function") return null;
  try {
    const pos = term.getSelectionPosition();
    if (!pos) return null;
    return { start: { y: pos.start.y }, end: { y: pos.end.y } };
  } catch {
    return null;
  }
}
