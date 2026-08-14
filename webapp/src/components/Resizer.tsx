import { useCallback, useRef } from "react";
import { beginColumnResize, endColumnResize } from "../lib/columnResize";

// A visually narrow divider keeps a wider pointer target for precise resizing.
// side="left" means it resizes the pane to its LEFT
// (delta = mouse dx); side="right" means the pane to its RIGHT (delta = -dx).
export default function Resizer({ onResize, side = "left" }: {
  onResize: (deltaPx: number) => void;
  side?: "left" | "right";
}) {
  const startX = useRef(0);
  const onDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    startX.current = e.clientX;
    beginColumnResize();
  }, []);

  const onMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    const dx = e.clientX - startX.current;
    startX.current = e.clientX;
    onResize(side === "left" ? dx : -dx);
  }, [onResize, side]);

  const onUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    endColumnResize();
  }, []);

  return (
    <div
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${side === "left" ? "left" : "right"} panel`}
      className="group relative w-[6px] shrink-0 self-stretch cursor-col-resize bg-transparent"
      style={{ zIndex: 10, touchAction: "none" }}
      title="Drag to resize"
    >
      <span
        className="pointer-events-none absolute inset-y-2 left-1/2 w-px -translate-x-1/2 rounded-full bg-transparent transition-colors group-hover:bg-accent2/50 group-focus-visible:bg-accent2/50"
        aria-hidden="true"
      />
      <span className="absolute -inset-x-2 inset-y-0 cursor-col-resize" aria-hidden="true" />
    </div>
  );
}
