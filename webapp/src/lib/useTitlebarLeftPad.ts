import { useLayoutEffect, useState } from "react";
import { TITLEBAR_TRAFFIC_PAD_PX, titlebarLeftPad } from "./titlebarSafe";

/** Left pad that clears the macOS traffic lights only where a top strip meets
 *  them. Pass the returned callback as the strip's `ref`. The pad is measured
 *  when the strip mounts and when the strip or the window changes size, so a
 *  strip in the chat card clears the lights only while the left rail is closed. */
export function useTitlebarLeftPad(): [number, (el: HTMLElement | null) => void] {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const [pad, setPad] = useState(TITLEBAR_TRAFFIC_PAD_PX);
  useLayoutEffect(() => {
    if (!el) return;
    const measure = () => setPad(titlebarLeftPad(el.getBoundingClientRect()));
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [el]);
  return [pad, setEl];
}
