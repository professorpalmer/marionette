import type { RefObject } from "react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { BookPlus, Clipboard, ClipboardPaste, Pencil, Scissors, TextSelect } from "lucide-react";
import { isMacNavigator } from "../../lib/terminalSelection";

type Spellcheck = { misspelledWord: string; suggestions: string[] };
type MenuState = { x: number; y: number; spellcheck: Spellcheck | null };
type EditCommand = "copy" | "cut" | "paste";

const itemClass = "flex w-full items-center gap-2 text-left px-3 py-1.5 text-txt hover:bg-panel2 transition-colors disabled:pointer-events-none disabled:opacity-40";

export default function ComposerContextMenu({
  textareaRef,
}: {
  textareaRef: RefObject<HTMLTextAreaElement | null>;
}) {
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const menuRef = useRef<HTMLDivElement>(null);
  const composerGesture = useRef(false);

  useEffect(() => {
    const markGesture = (event: MouseEvent) => {
      composerGesture.current = event.target === textareaRef.current;
    };
    window.addEventListener("contextmenu", markGesture, true);
    return () => window.removeEventListener("contextmenu", markGesture, true);
  }, [textareaRef]);

  useEffect(() => {
    const ipc = (window as any).harnessIPC;
    return ipc?.onContextMenuOpen?.((payload: Spellcheck & { x: number; y: number }) => {
      const belongsToComposer = composerGesture.current;
      composerGesture.current = false;
      if (!belongsToComposer) {
        void ipc.contextMenuNative?.();
        return;
      }
      setMenu({
        x: payload.x,
        y: payload.y,
        spellcheck: payload.misspelledWord ? payload : null,
      });
    });
  }, []);

  useLayoutEffect(() => {
    if (!menu || !menuRef.current) return;
    const bounds = menuRef.current.getBoundingClientRect();
    setPosition({
      left: Math.max(8, Math.min(menu.x, window.innerWidth - bounds.width - 8)),
      top: Math.max(8, Math.min(menu.y, window.innerHeight - bounds.height - 8)),
    });
  }, [menu]);

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [menu]);

  if (!menu) return null;

  const textarea = textareaRef.current;
  const hasSelection = Boolean(textarea && textarea.selectionStart !== textarea.selectionEnd);
  const hasText = Boolean(textarea?.value);
  const modifier = isMacNavigator() ? "⌘" : "Ctrl+";
  const run = (action: () => void) => {
    setMenu(null);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      action();
    });
  };
  const edit = (command: EditCommand) => run(() => {
    void (window as any).harnessIPC?.contextMenuEdit?.(command);
  });
  const spell = (kind: "add" | "replace", word: string) => run(() => {
    void (window as any).harnessIPC?.contextMenuSpellcheck?.({ kind, word });
  });

  return (
    <div
      ref={menuRef}
      role="menu"
      aria-label="Composer menu"
      className="fixed z-50 min-w-[220px] rounded border border-edge bg-panel py-1 text-[12px] text-txt shadow-lg"
      style={position}
      onMouseDown={(event) => event.stopPropagation()}
    >
      {menu.spellcheck && (
        <>
          {menu.spellcheck.suggestions.slice(0, 5).map((suggestion) => (
            <button key={suggestion} type="button" role="menuitem" className={itemClass} onClick={() => spell("replace", suggestion)}>
              <Pencil size={12} className="text-accent" />
              <span>{suggestion}</span>
            </button>
          ))}
          <button type="button" role="menuitem" className={itemClass} onClick={() => spell("add", menu.spellcheck!.misspelledWord)}>
            <BookPlus size={12} className="text-muted" />
            <span>Add to dictionary</span>
          </button>
          <div className="border-t border-edge my-1" />
        </>
      )}
      <button type="button" role="menuitem" className={itemClass} disabled={!hasSelection} onClick={() => edit("cut")}>
        <Scissors size={12} className="text-muted" />
        <span>Cut</span><span className="ml-auto font-mono text-[10px] text-faint">{modifier}X</span>
      </button>
      <button type="button" role="menuitem" className={itemClass} disabled={!hasSelection} onClick={() => edit("copy")}>
        <Clipboard size={12} className="text-muted" />
        <span>Copy</span><span className="ml-auto font-mono text-[10px] text-faint">{modifier}C</span>
      </button>
      <button type="button" role="menuitem" className={itemClass} onClick={() => edit("paste")}>
        <ClipboardPaste size={12} className="text-muted" />
        <span>Paste</span><span className="ml-auto font-mono text-[10px] text-faint">{modifier}V</span>
      </button>
      <div className="border-t border-edge my-1" />
      <button type="button" role="menuitem" className={itemClass} disabled={!hasText} onClick={() => run(() => textareaRef.current?.select())}>
        <TextSelect size={12} className="text-muted" />
        <span>Select all</span><span className="ml-auto font-mono text-[10px] text-faint">{modifier}A</span>
      </button>
    </div>
  );
}
