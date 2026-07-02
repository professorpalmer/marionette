import { Component, type ErrorInfo, type ReactNode } from "react";
import { RotateCw, AlertTriangle } from "lucide-react";

interface Props {
  children: ReactNode;
  /** Human label for the region that failed, e.g. "Swarm panel" or "app". */
  label?: string;
  /** When true, render a compact inline fallback instead of the full-screen one. */
  inline?: boolean;
}

interface State {
  error: Error | null;
  info: ErrorInfo | null;
}

/**
 * Catches render/lifecycle exceptions in its subtree so a single broken
 * component can never blank the entire Electron window (the "black screen and
 * I'm stuck" failure). React unmounts a subtree that throws during render; with
 * no boundary that subtree is the whole app. This shows a recoverable fallback
 * that surfaces the real error + stack, so the actual root cause is visible
 * instead of a void -- and offers Reload / Try again to get unstuck.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ info });
    // Route to the console (captured by devtools) and, when available, to the
    // Electron main log so a crash is diagnosable without opening devtools.
    const where = this.props.label || "app";
    console.error(`[ErrorBoundary:${where}]`, error, info?.componentStack);
    try {
      (window as any).harnessIPC?.logError?.({
        scope: where,
        message: String(error?.message || error),
        stack: error?.stack || "",
        componentStack: info?.componentStack || "",
      });
    } catch {
      // logging is best-effort; never let it mask the original error
    }
  }

  private reset = () => this.setState({ error: null, info: null });

  render() {
    const { error, info } = this.state;
    if (!error) return this.props.children;

    const label = this.props.label || "the app";

    if (this.props.inline) {
      return (
        <div className="flex flex-col items-center justify-center h-full min-h-[120px] gap-2 p-4 text-center bg-panel">
          <AlertTriangle size={18} className="text-risk" />
          <span className="text-[12px] text-txt font-medium">{label} hit an error</span>
          <span className="text-[10.5px] text-faint font-mono break-all max-w-full">
            {String(error.message || error)}
          </span>
          <button
            onClick={this.reset}
            className="mt-1 px-2.5 h-[24px] rounded-md bg-accent text-black/90 text-[10.5px] font-semibold flex items-center gap-1 hover:brightness-110"
          >
            <RotateCw size={11} /> Try again
          </button>
        </div>
      );
    }

    return (
      <div className="h-full w-full flex flex-col items-center justify-center gap-4 p-8 bg-bg text-txt overflow-auto">
        <AlertTriangle size={28} className="text-risk" />
        <div className="text-[15px] font-semibold">{label} crashed</div>
        <div className="text-[12px] text-muted max-w-[560px] text-center leading-relaxed">
          A UI error was caught before it could take down the whole window. Your
          session and backend are still running -- try again, or reload the view.
        </div>
        <div className="w-full max-w-[720px] rounded-lg border border-edge bg-panel p-3 text-[11px] font-mono text-risk/90 whitespace-pre-wrap break-words max-h-[260px] overflow-auto">
          {String(error.stack || error.message || error)}
          {info?.componentStack ? `\n\nComponent stack:${info.componentStack}` : ""}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={this.reset}
            className="px-3 h-[30px] rounded-md bg-accent text-black/90 text-[12px] font-semibold flex items-center gap-1.5 hover:brightness-110"
          >
            <RotateCw size={13} /> Try again
          </button>
          <button
            onClick={() => window.location.reload()}
            className="px-3 h-[30px] rounded-md bg-panel2 border border-edge text-txt text-[12px] font-medium hover:bg-panel2/70"
          >
            Reload window
          </button>
        </div>
      </div>
    );
  }
}
