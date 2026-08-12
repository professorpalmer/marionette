import { useCallback, useRef } from "react";

// A visually narrow divider keeps a wider pointer target for precise resizing.
// side="left" means it resizes the pane to its LEFT
// (delta = mouse dx); side="right" means the pane to its RIGHT (delta = -dx).
export default function Resizer({ onResize, side = "left" }: {
  onResize: (deltaPx: number) => void;
  side?: "left" | "right";
}) {
  const startX = useRef(0);
  const onDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    startX.current = e.clientX;
    const move = (ev: MouseEvent) => {
      const dx = ev.clientX - startX.current;
      startX.current = ev.clientX;
      onResize(side === "left" ? dx : -dx);
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, [onResize, side]);

  return (
    <div
      onMouseDown={onDown}
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${side === "left" ? "left" : "right"} panel`}
      className="group relative w-px shrink-0 self-stretch cursor-col-resize"
      style={{ zIndex: 10 }}
      title="Drag to resize"
    >
      <span
        className="pointer-events-none absolute inset-y-px left-1/2 w-px -translate-x-1/2 rounded-full bg-edge/30 transition-colors group-hover:bg-accent2/50"
        aria-hidden="true"
      />
      <span className="absolute -inset-x-1.5 inset-y-0 cursor-col-resize" aria-hidden="true" />
    </div>
  );
}
