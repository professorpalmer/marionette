import { useEffect, useRef, useState } from "react";
import { Loader2, Save, RotateCcw, Wand2 } from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { okaidia } from "@uiw/codemirror-theme-okaidia";
import { keymap, EditorView } from "@codemirror/view";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { json } from "@codemirror/lang-json";
import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { markdown } from "@codemirror/lang-markdown";
import { api } from "../lib/api";

interface FileEditorPaneProps {
  path: string;
  /** 1-based line to reveal after load (agent-loop link clicks). */
  line?: number;
  /** 1-based column within the line (optional). */
  col?: number;
  onClose: () => void;
  onDirtyChange: (dirty: boolean) => void;
}

function getLanguageExtension(filePath: string) {
  const ext = filePath.split(".").pop()?.toLowerCase() || "";
  switch (ext) {
    case "ts":
    case "tsx":
    case "js":
    case "jsx":
    case "mjs":
    case "cjs":
      return javascript({ jsx: true, typescript: true });
    case "py":
      return python();
    case "json":
      return json();
    case "css":
    case "scss":
      return css();
    case "html":
      return html();
    case "md":
    case "markdown":
      return markdown();
    default:
      return null;
  }
}

/** Scroll CodeMirror to a 1-based line (and optional 1-based column). */
function _scrollEditorToLine(view: EditorView, line: number, col?: number): void {
  try {
    const doc = view.state.doc;
    const ln = Math.max(1, Math.min(line, doc.lines));
    const lineObj = doc.line(ln);
    const c = col != null ? Math.max(0, Math.min(col - 1, lineObj.length)) : 0;
    const pos = lineObj.from + c;
    view.dispatch({
      selection: { anchor: pos, head: pos },
      effects: EditorView.scrollIntoView(pos, { y: "center" }),
    });
    view.focus();
  } catch {
    /* ignore */
  }
}

function getLanguageFromPath(filePath: string): string {
  const ext = filePath.split(".").pop()?.toLowerCase() || "";
  switch (ext) {
    case "ts":
    case "tsx":
      return "typescript";
    case "js":
    case "jsx":
      return "javascript";
    case "py":
      return "python";
    case "json":
      return "json";
    case "css":
      return "css";
    case "html":
      return "html";
    case "md":
    case "markdown":
      return "markdown";
    default:
      return "";
  }
}

const customTheme = EditorView.theme({
  "&": {
    fontSize: "13px",
    height: "100%",
  },
  ".cm-scroller": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace",
  },
});

export default function FileEditorPane({ path, line, col, onClose, onDirtyChange }: FileEditorPaneProps) {
  const [content, setContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [readOnly, setReadOnly] = useState(false);

  const [showInlinePrompt, setShowInlinePrompt] = useState(false);
  const [inlineInstruction, setInlineInstruction] = useState("");
  const [inlineRange, setInlineRange] = useState<{ from: number; to: number } | null>(null);
  const [inlineLoading, setInlineLoading] = useState(false);
  const [inlineError, setInlineError] = useState<string | null>(null);
  const editorViewRef = useRef<EditorView | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const pendingJumpRef = useRef<{ line?: number; col?: number } | null>(
    line != null ? { line, col } : null
  );

  // Agent-loop links may re-open the same file at a new line.
  useEffect(() => {
    if (line == null) return;
    pendingJumpRef.current = { line, col };
    const view = editorViewRef.current;
    if (view && !loading) {
      _scrollEditorToLine(view, line, col);
      pendingJumpRef.current = null;
    }
  }, [line, col, path, loading]);

  useEffect(() => {
    if (showInlinePrompt && inputRef.current) {
      inputRef.current.focus();
    }
  }, [showInlinePrompt]);

  useEffect(() => {
    if (!showInlinePrompt) return;
    const handleOutsideClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setShowInlinePrompt(false);
        setInlineRange(null);
      }
    };
    document.addEventListener("mousedown", handleOutsideClick);
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
    };
  }, [showInlinePrompt]);

  const handleInlineEditSubmit = async () => {
    if (!inlineInstruction.trim() || !inlineRange || !editorViewRef.current) return;
    setInlineLoading(true);
    setInlineError(null);
    const view = editorViewRef.current;
    const { from, to } = inlineRange;
    const docLen = view.state.doc.length;

    const prefix = view.state.sliceDoc(Math.max(0, from - 2000), from);
    const suffix = view.state.sliceDoc(to, Math.min(docLen, to + 2000));
    const selection = view.state.sliceDoc(from, to);
    const language = getLanguageFromPath(path);

    try {
      const res = await api.inlineEdit(
        path,
        selection,
        inlineInstruction,
        prefix,
        suffix,
        language
      );
      if (res.ok && res.edit !== undefined) {
        const replacement = res.edit;
        view.dispatch({
          changes: { from, to, insert: replacement }
        });

        // set selection to the newly inserted range to highlight it
        const newTo = from + replacement.length;
        view.dispatch({
          selection: { anchor: from, head: newTo },
          scrollIntoView: true
        });

        // Mark dirty
        setContent(view.state.doc.toString());
        setIsDirty(true);
        onDirtyChangeRef.current(true);

        // Close prompt
        setShowInlinePrompt(false);
        setInlineRange(null);
      } else {
        setInlineError(res.error || "Failed to process inline edit");
      }
    } catch (err: any) {
      setInlineError(err.message || "Error performing inline edit");
    } finally {
      setInlineLoading(false);
    }
  };

  const onDirtyChangeRef = useRef(onDirtyChange);
  useEffect(() => {
    onDirtyChangeRef.current = onDirtyChange;
  });

  useEffect(() => {
    let active = true;
    async function loadFile() {
      setLoading(true);
      setError(null);
      try {
        const res = await api.readFile(path);
        if (!active) return;
        if (res.ok) {
          setContent(res.content || "");
          setOriginalContent(res.content || "");
          setReadOnly(!!res.truncated);
          setIsDirty(false);
          onDirtyChangeRef.current(false);
          // Jump after content lands; onCreateEditor may also apply pendingJump.
          if (pendingJumpRef.current?.line != null) {
            // Defer until CodeMirror has the new doc.
            requestAnimationFrame(() => {
              const view = editorViewRef.current;
              const jump = pendingJumpRef.current;
              if (view && jump?.line != null) {
                _scrollEditorToLine(view, jump.line, jump.col);
                pendingJumpRef.current = null;
              }
            });
          }
        } else if (res.binary) {
          setError("Binary file cannot be viewed or edited in-app");
        } else {
          setError(res.error || "Failed to read file");
        }
      } catch (err: any) {
        if (active) {
          setError(err.message || "Error reading file contents");
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }
    loadFile();
    return () => {
      active = false;
    };
  }, [path]);

  const handleSave = async (currentContent: string = content) => {
    if (saving || !isDirty) return;
    setSaving(true);
    setSaveStatus("saving");
    setError(null);
    try {
      const res = await api.writeFile(path, currentContent);
      if (res.ok) {
        setSaveStatus("saved");
        setIsDirty(false);
        onDirtyChangeRef.current(false);
        setOriginalContent(currentContent);
        // Let other components know (like workspace files tree or git view)
        window.dispatchEvent(new CustomEvent("harness-file-saved", { detail: { path } }));
        setTimeout(() => setSaveStatus("idle"), 2000);
      } else {
        setSaveStatus("error");
        setError(res.error || "Failed to save file");
      }
    } catch (err: any) {
      setSaveStatus("error");
      setError(err.message || "Error saving file");
    } finally {
      setSaving(false);
    }
  };

  const handleSaveRef = useRef(handleSave);
  useEffect(() => {
    handleSaveRef.current = handleSave;
  });

  const handleRevert = () => {
    if (window.confirm("Discard all local edits and restore file from disk?")) {
      setContent(originalContent);
      setIsDirty(false);
      onDirtyChangeRef.current(false);
    }
  };

  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "s") {
        e.preventDefault();
        handleSave(content);
      }
    };
    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => {
      window.removeEventListener("keydown", handleGlobalKeyDown);
    };
  }, [content, isDirty, saving, path, originalContent]);

  if (loading) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center bg-bg">
        <Loader2 className="animate-spin text-accent mb-2" size={24} />
        <span className="text-[12px] text-muted">Reading file...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center bg-bg px-6 text-center">
        <span className="text-risk font-semibold text-[13px] mb-2">{error}</span>
        <button
          onClick={onClose}
          className="text-[11px] text-muted hover:text-txt underline transition-colors"
        >
          Close editor
        </button>
      </div>
    );
  }

  // Set up CodeMirror extensions dynamically
  const extensions = [customTheme];
  const langExt = getLanguageExtension(path);
  if (langExt) {
    extensions.push(langExt);
  }

  // Handle Cmd+S/Ctrl+S keymap within CodeMirror
  extensions.push(
    keymap.of([
      {
        key: "Mod-s",
        run: (view) => {
          const currentContent = view.state.doc.toString();
          handleSaveRef.current(currentContent);
          return true;
        },
      },
      {
        key: "Mod-k",
        run: (view) => {
          if (readOnly) return true;
          editorViewRef.current = view;
          let { from, to } = view.state.selection.main;
          if (from === to) {
            const line = view.state.doc.lineAt(from);
            from = line.from;
            to = line.to;
          }
          setInlineRange({ from, to });
          setInlineInstruction("");
          setInlineError(null);
          setShowInlinePrompt(true);
          return true;
        },
      },
    ])
  );

  return (
    <div className="flex-1 flex flex-col bg-bg h-full min-h-0 overflow-hidden relative">
      {/* Editor toolbar */}
      <div className="flex items-center justify-between px-4 py-1.5 border-b border-edge bg-panel select-none shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[11px] font-mono text-muted truncate" title={path}>
            {path}
          </span>
          {isDirty && (
            <span className="w-2 h-2 rounded-full bg-warn shrink-0" title="Unsaved changes" />
          )}
          {readOnly && (
            <span className="px-1.5 py-0.5 rounded bg-panel2 border border-edge text-[9px] font-mono uppercase text-muted tracking-wider select-none shrink-0">
              Read-only
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          {saveStatus === "saving" && (
            <span className="text-[11px] text-muted flex items-center gap-1">
              <Loader2 className="animate-spin" size={12} />
              Saving...
            </span>
          )}
          {saveStatus === "saved" && (
            <span className="text-[11px] text-good">Saved</span>
          )}
          {saveStatus === "error" && (
            <span className="text-[11px] text-risk">Save failed</span>
          )}

          <div className="flex items-center gap-1.5">
            <button
              onClick={handleRevert}
              disabled={!isDirty || saving}
              className={`flex items-center gap-1 px-2 py-1 rounded text-[11px] transition-colors border ${
                isDirty && !saving
                  ? "border-edge text-muted hover:text-txt hover:bg-panel2"
                  : "border-transparent text-faint cursor-not-allowed"
              }`}
              title="Discard unsaved changes"
            >
              <RotateCcw size={12} />
              Revert
            </button>
            <button
              onClick={() => handleSave(content)}
              disabled={!isDirty || saving}
              className={`flex items-center gap-1 px-2.5 py-1 rounded text-[11px] transition-colors border ${
                isDirty && !saving
                  ? "bg-accent/15 border-accent/30 text-accent hover:bg-accent/25"
                  : "border-transparent text-faint cursor-not-allowed"
              }`}
              title="Save file (Cmd/Ctrl+S)"
            >
              <Save size={12} />
              Save
            </button>
          </div>
        </div>
      </div>

      {/* Editor area with CodeMirror */}
      <div className="flex-1 overflow-hidden relative">
        {showInlinePrompt && (
          <div ref={containerRef} className="absolute top-2 right-4 z-50 w-96 bg-panel2 border border-edge rounded-md shadow-lg p-3 flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold text-accent uppercase tracking-wider flex items-center gap-1">
                <Wand2 size={12} className="text-accent shrink-0 animate-pulse" />
                Inline Edit
              </span>
              <button
                onClick={() => {
                  setShowInlinePrompt(false);
                  setInlineRange(null);
                }}
                className="text-[10px] text-muted hover:text-txt transition-colors border border-edge rounded px-1.5 py-0.5 bg-panel"
              >
                Esc
              </button>
            </div>
            <div className="relative flex items-center">
              <input
                ref={inputRef}
                type="text"
                value={inlineInstruction}
                onChange={(e) => setInlineInstruction(e.target.value)}
                placeholder="Describe the edit... (Enter to apply)"
                className="w-full bg-panel border border-edge rounded px-2.5 py-1.5 text-[12px] text-txt placeholder:text-muted outline-none focus:border-accent transition-colors"
                disabled={inlineLoading}
                onKeyDown={async (e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    await handleInlineEditSubmit();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    setShowInlinePrompt(false);
                    setInlineRange(null);
                  }
                }}
              />
            </div>
            {inlineLoading && (
              <div className="text-[11px] text-muted flex items-center gap-1.5 py-0.5">
                <Loader2 className="animate-spin text-accent" size={12} />
                Thinking...
              </div>
            )}
            {inlineError && (
              <div className="text-[11px] text-risk break-words font-medium py-0.5">
                {inlineError}
              </div>
            )}
          </div>
        )}
        <CodeMirror
          value={content}
          theme={okaidia}
          height="100%"
          className="h-full text-[13px]"
          extensions={extensions}
          onCreateEditor={(view) => {
            editorViewRef.current = view;
            const jump = pendingJumpRef.current;
            if (jump?.line != null) {
              _scrollEditorToLine(view, jump.line, jump.col);
              pendingJumpRef.current = null;
            }
          }}
          onChange={(val) => {
            setContent(val);
            setIsDirty(true);
            onDirtyChangeRef.current(true);
          }}
          readOnly={readOnly}
          basicSetup={{
            lineNumbers: true,
            highlightActiveLine: true,
            bracketMatching: true,
            foldGutter: false,
            dropCursor: true,
            allowMultipleSelections: true,
            indentOnInput: true,
          }}
        />
      </div>
    </div>
  );
}
