/**
 * Feed-only Motion helpers (v0.9.318). Transcript enter/exit + layout polish;
 * not used app-wide. Respects prefers-reduced-motion via Motion's hook.
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

type FeedMotionPresenceProps = {
  children: ReactNode;
};

/** popLayout wrapper for transcript rows — virtual window, fallback, live tail. */
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

/** One feed row shell: motion.div layout for popLayout reflow. */
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
