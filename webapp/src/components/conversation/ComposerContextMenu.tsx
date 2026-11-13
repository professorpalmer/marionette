import type { RefObject } from "react";
import { useEffect, useRef, useState } from "react";
import { Menu } from "@base-ui/react/menu";
import { BookPlus, Clipboard, ClipboardPaste, Pencil, Scissors, TextSelect } from "lucide-react";
import { isMacNavigator } from "../../lib/terminalSelection";
import { MenuPanel, MenuRoot, pointAnchor } from "../ui/overlay";

type Spellcheck = { misspelledWord: string; suggestions: string[] };
type MenuState = { x: number; y: number; spellcheck: Spellcheck | null; open: boolean };
type EditCommand = "copy" | "cut" | "paste";

const itemClass = "flex w-full items-center gap-2 text-left px-3 py-1.5 text-txt hover:bg-panel2 data-[highlighted]:bg-panel2 outline-none transition-colors data-[disabled]:pointer-events-none data-[disabled]:opacity-40";

export default function ComposerContextMenu({
  textareaRef,
}: {
  textareaRef: RefObject<HTMLTextAreaElement | null>;
}) {
  const [menu, setMenu] = useState<MenuState | null>(null);
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
        open: true,
      });
    });
  }, []);

  const textarea = textareaRef.current;
  const hasSelection = Boolean(textarea && textarea.selectionStart !== textarea.selectionEnd);
  const hasText = Boolean(textarea?.value);
  const modifier = isMacNavigator() ? "⌘" : "Ctrl+";
  const close = () => setMenu((current) => (current ? { ...current, open: false } : null));
  const run = (action: () => void) => {
    close();
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
    <MenuRoot open={menu?.open ?? false} onOpenChange={(next) => { if (!next) close(); }}>
      {menu && (
        <MenuPanel
          label="Composer menu"
          anchor={pointAnchor(menu.x, menu.y)}
          sideOffset={0}
          finalFocus={textareaRef}
          className="min-w-[220px] rounded border border-edge bg-panel py-1 text-ui-12 text-txt shadow-lg outline-none"
        >
          {menu.spellcheck && (
            <>
              {menu.spellcheck.suggestions.slice(0, 5).map((suggestion) => (
                <Menu.Item key={suggestion} className={itemClass} onClick={() => spell("replace", suggestion)}>
                  <Pencil size={12} className="text-accent" />
                  <span>{suggestion}</span>
                </Menu.Item>
              ))}
              <Menu.Item className={itemClass} onClick={() => spell("add", menu.spellcheck!.misspelledWord)}>
                <BookPlus size={12} className="text-muted" />
                <span>Add to dictionary</span>
              </Menu.Item>
              <Menu.Separator className="border-t border-edge my-1" />
            </>
          )}
          <Menu.Item className={itemClass} disabled={!hasSelection} onClick={() => edit("cut")}>
            <Scissors size={12} className="text-muted" />
            <span>Cut</span><span className="ml-auto font-mono text-ui-10 text-faint">{modifier}X</span>
          </Menu.Item>
          <Menu.Item className={itemClass} disabled={!hasSelection} onClick={() => edit("copy")}>
            <Clipboard size={12} className="text-muted" />
            <span>Copy</span><span className="ml-auto font-mono text-ui-10 text-faint">{modifier}C</span>
          </Menu.Item>
          <Menu.Item className={itemClass} onClick={() => edit("paste")}>
            <ClipboardPaste size={12} className="text-muted" />
            <span>Paste</span><span className="ml-auto font-mono text-ui-10 text-faint">{modifier}V</span>
          </Menu.Item>
          <Menu.Separator className="border-t border-edge my-1" />
          <Menu.Item className={itemClass} disabled={!hasText} onClick={() => run(() => textareaRef.current?.select())}>
            <TextSelect size={12} className="text-muted" />
            <span>Select all</span><span className="ml-auto font-mono text-ui-10 text-faint">{modifier}A</span>
          </Menu.Item>
        </MenuPanel>
      )}
    </MenuRoot>
  );
}
