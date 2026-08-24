/**
 * Feed-only Motion helpers (v0.9.318 / v0.9.329). Transcript enter/exit +
 * layout polish on in-flow rows; not used app-wide. Virtual window rows must
 * never take a Motion layout transform — TanStack owns translateY.
 * prefers-reduced-motion stays off (animations run unless the user opted out).
 */

import { forwardRef, memo, type CSSProperties, type ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";

/** Whether feed layout/presence animations should run. */
export function feedLayoutMotionEnabled(reducedMotion: boolean | null): boolean {
  return !reducedMotion;
}

/** Whether feed layout/presence animations should run. */
export function useFeedLayoutMotion(): boolean {
  return feedLayoutMotionEnabled(useReducedMotion());
}

/**
 * Virtual window rows are `absolute top-0` + virtualizer translateY.
 * Motion `layout` / popLayout write `transform` and stack every row at y=0.
 */
export const VIRTUAL_ROW_LAYOUT_ENABLED = false;

type FeedMotionPresenceProps = {
  children: ReactNode;
};

/** popLayout wrapper for in-flow transcript rows (fallback list + live tail). */
export const FeedMotionPresence = memo(function FeedMotionPresence({
  children,
}: FeedMotionPresenceProps) {
  return (
    <AnimatePresence mode="popLayout" initial={false}>
      {children}
    </AnimatePresence>
  );
});

type FeedMotionRowProps = {
  layoutEnabled: boolean;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
  "data-index"?: number;
  "data-testid"?: string;
  "data-dom-measure"?: string;
};

/** One feed row shell: motion.div layout only when layoutEnabled. */
export const FeedMotionRow = memo(
  forwardRef<HTMLDivElement, FeedMotionRowProps>(function FeedMotionRow(
    {
      layoutEnabled,
      className,
      style,
      children,
      "data-index": dataIndex,
      "data-testid": testId,
      "data-dom-measure": domMeasure,
    },
    ref,
  ) {
    return (
      <motion.div
        ref={ref}
        layout={layoutEnabled}
        initial={false}
        className={className}
        style={style}
        data-index={dataIndex}
        data-testid={testId}
        data-dom-measure={domMeasure}
      >
        {children}
      </motion.div>
    );
  }),
);
