import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2, Save, RotateCcw, Wand2, Eye, Code2, Globe, X } from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { okaidia } from "@uiw/codemirror-theme-okaidia";
import { keymap, EditorView } from "@codemirror/view";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { json } from "@codemirror/lang-json";
import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { markdown } from "@codemirror/lang-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../lib/api";
import { isDesktop, revealInFolderLabel, revealWorkspacePath } from "../lib/transport";

interface FileEditorPaneProps {
  path: string;
  /** 1-based line to reveal after load (agent-loop link clicks). */
  line?: number;
  /** 1-based column within the line (optional). */
  col?: number;
  onClose: () => void;
  onDirtyChange: (dirty: boolean) => void;
}

type EditorKind = "code" | "markdown" | "html" | "pdf" | "image" | "binary";
type TextBinaryMode = "code" | "preview";

type BinaryMeta = {
  name?: string;
  size?: number;
  mime?: string;
  ext?: string;
  sqlite_tables?: string[];
};

function fileExt(filePath: string): string {
  const base = filePath.split(/[/\\]/).pop() || filePath;
  const i = base.lastIndexOf(".");
  return i >= 0 ? base.slice(i + 1).toLowerCase() : "";
}

function detectEditorKind(filePath: string, binary?: boolean): EditorKind {
  const ext = fileExt(filePath);
  if (binary) {
    if (ext === "pdf") return "pdf";
    if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"].includes(ext)) return "image";
    return "binary";
  }
  if (ext === "md" || ext === "markdown") return "markdown";
  if (ext === "html" || ext === "htm") return "html";
  // Text sources stay editable in CodeMirror; raster/PDF use viewers only when binary.
  return "code";
}

function formatBytes(n?: number): string {
  if (n == null || !Number.isFinite(n)) return "unknown size";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function getLanguageExtension(filePath: string) {
  const ext = fileExt(filePath);
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
    case "htm":
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
  const ext = fileExt(filePath);
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
    case "htm":
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
  const [kind, setKind] = useState<EditorKind>("code");
  const [textMode, setTextMode] = useState<TextBinaryMode>("code");
  const [binaryMeta, setBinaryMeta] = useState<BinaryMeta | null>(null);
  const [repoRoot, setRepoRoot] = useState("");
  const [pathMenu, setPathMenu] = useState<{ x: number; y: number } | null>(null);

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

  const rawUrl = useMemo(() => api.fileRawUrl(path), [path]);
  const canPreview = kind === "markdown" || kind === "html";
  const isTextEditable = kind === "code" || kind === "markdown" || kind === "html";

  // Agent-loop links may re-open the same file at a new line.
  useEffect(() => {
    if (line == null) return;
    pendingJumpRef.current = { line, col };
    const view = editorViewRef.current;
    if (view && !loading && textMode === "code") {
      _scrollEditorToLine(view, line, col);
      pendingJumpRef.current = null;
    }
  }, [line, col, path, loading, textMode]);

  useEffect(() => {
    let cancelled = false;
    void api.config().then((cfg) => {
      if (!cancelled) setRepoRoot(cfg.repo || "");
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!pathMenu) return;
    const close = () => setPathMenu(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("click", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [pathMenu]);

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

        const newTo = from + replacement.length;
        view.dispatch({
          selection: { anchor: from, head: newTo },
          scrollIntoView: true
        });

        setContent(view.state.doc.toString());
        setIsDirty(true);
        onDirtyChangeRef.current(true);

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
      setBinaryMeta(null);
      setTextMode("code");
      try {
        const res = await api.readFile(path);
        if (!active) return;
        if (res.ok) {
          const detected = detectEditorKind(path, false);
          setKind(detected);
          // HTML defaults to preview; markdown stays in code until toggled.
          setTextMode(detected === "html" ? "preview" : "code");
          setContent(res.content || "");
          setOriginalContent(res.content || "");
          setReadOnly(!!res.truncated);
          setIsDirty(false);
          onDirtyChangeRef.current(false);
          if (pendingJumpRef.current?.line != null) {
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
          const detected = detectEditorKind(path, true);
          setKind(detected);
          setBinaryMeta({
            name: res.name,
            size: res.size,
            mime: res.mime,
            ext: res.ext,
            sqlite_tables: res.sqlite_tables,
          });
          setContent("");
          setOriginalContent("");
          setReadOnly(true);
          setIsDirty(false);
          onDirtyChangeRef.current(false);
          setError(null);
        } else {
          setKind(detectEditorKind(path, false));
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
    if (saving || !isDirty || !isTextEditable) return;
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

  const openInBrowserPanel = () => {
    let url = rawUrl;
    if (url && !/^https?:\/\//i.test(url) && typeof window !== "undefined") {
      url = `${window.location.origin}${url.startsWith("/") ? "" : "/"}${url}`;
    }
    try {
      (window as any).__pmPendingBrowserUrl = url;
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "browser" }));
      window.dispatchEvent(new CustomEvent("harness-open-url", { detail: { url } }));
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (isTextEditable) handleSave(content);
      }
    };
    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => window.removeEventListener("keydown", handleGlobalKeyDown);
  }, [content, isDirty, saving, path, originalContent, isTextEditable]);

  if (loading) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center bg-bg">
        <Loader2 className="animate-spin text-accent mb-2" size={24} />
        <span className="text-[12px] text-muted">Reading file...</span>
      </div>
    );
  }

  if (error && kind !== "binary" && kind !== "pdf" && kind !== "image") {
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

  const extensions = [customTheme];
  const langExt = getLanguageExtension(path);
  if (langExt) {
    extensions.push(langExt);
  }

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
          if (readOnly || !isTextEditable) return true;
          editorViewRef.current = view;
          let { from, to } = view.state.selection.main;
          if (from === to) {
            const lineObj = view.state.doc.lineAt(from);
            from = lineObj.from;
            to = lineObj.to;
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

  const showCodeMirror = isTextEditable && textMode === "code";
  const showMarkdownPreview = kind === "markdown" && textMode === "preview";
  const showHtmlPreview = kind === "html" && textMode === "preview";

  return (
    <div className="flex-1 flex flex-col bg-bg h-full min-h-0 overflow-hidden relative">
      {pathMenu && isDesktop && (
        <div
          className="fixed z-50 bg-panel border border-edge rounded shadow-lg text-[12px] py-1 min-w-[160px]"
          style={{ top: pathMenu.y, left: pathMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={async () => {
              setPathMenu(null);
              const res = await revealWorkspacePath(repoRoot, path);
              if (!res.ok) {
                window.dispatchEvent(
                  new CustomEvent("harness-toast", {
                    detail: res.error || "Could not reveal path",
                  }),
                );
              }
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            {revealInFolderLabel()}
          </button>
        </div>
      )}
      <div className="flex items-center justify-between px-4 py-1.5 border-b border-edge bg-panel select-none shrink-0 gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="text-[11px] font-mono text-muted truncate"
            title={path}
            onContextMenu={(e) => {
              if (!isDesktop) return;
              e.preventDefault();
              e.stopPropagation();
              setPathMenu({ x: e.clientX, y: e.clientY });
            }}
          >
            {path}
          </span>
          {isDirty && (
            <span className="w-2 h-2 rounded-full bg-warn shrink-0" title="Unsaved changes" />
          )}
          {(readOnly || !isTextEditable) && (
            <span className="px-1.5 py-0.5 rounded bg-panel2 border border-edge text-[9px] font-mono uppercase text-muted tracking-wider select-none shrink-0">
              Read-only
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {canPreview && (
            <div className="flex items-center rounded border border-edge overflow-hidden">
              <button
                type="button"
                onClick={() => setTextMode("code")}
                className={`flex items-center gap-1 px-2 py-1 text-[11px] transition-colors ${
                  textMode === "code" ? "bg-panel2 text-txt" : "text-muted hover:text-txt"
                }`}
                title="Source"
              >
                <Code2 size={12} />
                Code
              </button>
              <button
                type="button"
                onClick={() => setTextMode("preview")}
                className={`flex items-center gap-1 px-2 py-1 text-[11px] transition-colors border-l border-edge ${
                  textMode === "preview" ? "bg-panel2 text-txt" : "text-muted hover:text-txt"
                }`}
                title="Preview"
              >
                <Eye size={12} />
                Preview
              </button>
            </div>
          )}

          {(kind === "html" || kind === "pdf" || kind === "image") && (
            <button
              type="button"
              onClick={openInBrowserPanel}
              className="flex items-center gap-1 px-2 py-1 rounded text-[11px] border border-edge text-muted hover:text-txt hover:bg-panel2 transition-colors"
              title="Open in browser panel"
            >
              <Globe size={12} />
              Browser
            </button>
          )}

          {isTextEditable && (
            <>
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
            </>
          )}

          {!isTextEditable && (
            <button
              type="button"
              onClick={onClose}
              className="flex items-center gap-1 px-2 py-1 rounded text-[11px] border border-edge text-muted hover:text-txt hover:bg-panel2 transition-colors"
              title="Close editor"
            >
              <X size={12} />
              Close
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-hidden relative min-h-0">
        {showCodeMirror && (
          <>
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
          </>
        )}

        {showMarkdownPreview && (
          <div className="h-full overflow-auto px-6 py-4 text-txt">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                h1: ({ children }: any) => (
                  <h1 className="text-base font-semibold text-txt mt-3 mb-2 border-b border-edge pb-1">{children}</h1>
                ),
                h2: ({ children }: any) => (
                  <h2 className="text-[13px] font-semibold text-txt mt-3 mb-1.5">{children}</h2>
                ),
                h3: ({ children }: any) => (
                  <h3 className="text-[12px] font-semibold text-muted mt-2 mb-1">{children}</h3>
                ),
                p: ({ children }: any) => (
                  <p className="text-[13px] leading-relaxed my-2 first:mt-0 last:mb-0">{children}</p>
                ),
                ul: ({ children }: any) => (
                  <ul className="list-disc pl-5 my-2 space-y-1 text-txt/90">{children}</ul>
                ),
                ol: ({ children }: any) => (
                  <ol className="list-decimal pl-5 my-2 space-y-1 text-txt/90">{children}</ol>
                ),
                li: ({ children }: any) => (
                  <li className="text-[13px] leading-relaxed">{children}</li>
                ),
                code: ({ className, children }: any) => {
                  const inline = !className;
                  if (inline) {
                    return (
                      <code className="bg-panel2 px-1 py-0.5 rounded text-[12px] font-mono text-accent/90">
                        {children}
                      </code>
                    );
                  }
                  return (
                    <pre className="bg-panel2 border border-edge rounded p-3 my-2 overflow-auto text-[12px] font-mono">
                      <code className={className}>{children}</code>
                    </pre>
                  );
                },
                a: ({ href, children }: any) => (
                  <a href={href} className="text-accent/90 hover:underline" target="_blank" rel="noreferrer">
                    {children}
                  </a>
                ),
                blockquote: ({ children }: any) => (
                  <blockquote className="border-l-2 border-edge pl-3 my-2 text-muted italic">{children}</blockquote>
                ),
                hr: () => <hr className="border-edge/60 my-3" />,
              }}
            >
              {content}
            </ReactMarkdown>
          </div>
        )}

        {showHtmlPreview && (
          <iframe
            title="HTML preview"
            srcDoc={content}
            sandbox="allow-same-origin"
            className="w-full h-full border-0 bg-white"
          />
        )}

        {kind === "pdf" && (
          <iframe
            title="PDF preview"
            src={rawUrl}
            className="w-full h-full border-0 bg-panel"
          />
        )}

        {kind === "image" && (
          <div className="h-full overflow-auto flex items-center justify-center p-6 bg-bg">
            <img
              src={rawUrl}
              alt={binaryMeta?.name || path}
              className="max-w-full max-h-full object-contain"
            />
          </div>
        )}

        {kind === "binary" && (
          <div className="h-full overflow-auto flex flex-col items-center justify-center px-8 py-10 text-center gap-3">
            <div className="text-[13px] font-medium text-txt">
              {binaryMeta?.name || path.split(/[/\\]/).pop() || path}
            </div>
            <div className="text-[12px] text-muted font-mono">
              {formatBytes(binaryMeta?.size)}
              {binaryMeta?.mime ? ` · ${binaryMeta.mime}` : ""}
              {binaryMeta?.ext ? ` · ${binaryMeta.ext}` : ""}
            </div>
            <p className="text-[12px] text-muted max-w-md leading-relaxed">
              This file exists in the workspace, but a binary preview is not available in the editor.
            </p>
            {binaryMeta?.sqlite_tables && binaryMeta.sqlite_tables.length > 0 && (
              <div className="mt-2 w-full max-w-md text-left rounded border border-edge bg-panel2 px-3 py-2">
                <div className="text-[10px] uppercase tracking-wider text-muted mb-1.5">
                  SQLite tables
                </div>
                <ul className="text-[12px] font-mono text-txt space-y-0.5 max-h-40 overflow-auto">
                  {binaryMeta.sqlite_tables.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
            )}
            {binaryMeta?.sqlite_tables && binaryMeta.sqlite_tables.length === 0 && (
              <p className="text-[11px] text-muted">SQLite database (no tables listed).</p>
            )}
            <button
              type="button"
              onClick={onClose}
              className="mt-2 text-[11px] text-muted hover:text-txt underline transition-colors"
            >
              Close editor
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
