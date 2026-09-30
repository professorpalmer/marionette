import { useLayoutEffect, useState } from "react";

/** Gap kept between transcript text and the floating dock pill. */
export const DOCK_CLEARANCE_GAP_PX = 8;

/**
 * Right padding for a centred column so its text clears the floating dock
 * pill. Only an actual overlap adds padding: a constant reserve would push the
 * centred column off-centre on wide windows where the pill never touches it.
 */
export function dockClearancePad(opts: { columnRight: number; basePad: number; pillLeft: number | null }): number {
  if (opts.pillLeft == null) return opts.basePad;
  const overlap = opts.columnRight - opts.basePad - (opts.pillLeft - DOCK_CLEARANCE_GAP_PX);
  return opts.basePad + Math.max(0, overlap);
}

/**
 * Measured paddingRight for the element given to the returned callback ref,
 * or undefined while it needs none. The pill is looked up on each measure, so
 * a dock mounted after the column is still found on the next resize.
 */
export function useDockClearance(): [number | undefined, (el: HTMLElement | null) => void] {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const [pad, setPad] = useState<number | undefined>(undefined);
  useLayoutEffect(() => {
    if (!el) return;
    const measure = () => {
      const pill = document.querySelector('[data-testid="floating-dock-pill"]');
      const basePad = parseFloat(getComputedStyle(el).paddingLeft) || 0;
      const next = dockClearancePad({
        columnRight: el.getBoundingClientRect().right,
        basePad,
        pillLeft: pill ? pill.getBoundingClientRect().left : null,
      });
      setPad(next > basePad ? next : undefined);
    };
    measure();
    const frame = requestAnimationFrame(measure);
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    // A centred max-width column moves without resizing when the panels or
    // window change, so watch its scroll parent and the pill too.
    for (const target of [el, el.parentElement, document.querySelector('[data-testid="floating-dock-pill"]')]) {
      if (target) ro?.observe(target);
    }
    window.addEventListener("resize", measure);
    return () => {
      cancelAnimationFrame(frame);
      ro?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [el]);
  return [pad, setEl];
}
