import type { KeyboardEvent, RefObject } from "react";
import { Check } from "lucide-react";
import type { ReasoningEffort } from "../lib/api";
import { REASONING_LEVELS } from "../lib/reasoningSupport";

/** Reasoning-level rows for a picker menu. Pass ``selectedRef`` as the
 * overlay's initial focus so the keyboard starts on the current level. */
export default function ReasoningLevelOptions({
  value,
  onSelect,
  selectedRef,
}: {
  value: ReasoningEffort;
  onSelect: (level: ReasoningEffort) => void;
  selectedRef?: RefObject<HTMLButtonElement | null>;
}) {
  const moveFocus = (e: KeyboardEvent<HTMLButtonElement>) => {
    const rows = Array.from(
      e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [],
    );
    const at = rows.indexOf(e.currentTarget);
    const n = rows.length;
    const next = e.key === "ArrowDown" ? (at + 1) % n
      : e.key === "ArrowUp" ? (at - 1 + n) % n
        : e.key === "Home" ? 0
          : e.key === "End" ? n - 1
            : -1;
    if (next < 0) return;
    e.preventDefault();
    rows[next].focus();
  };

  return REASONING_LEVELS.map(({ value: level, label }) => {
    const isSelected = level === value;
    return (
      <button
        type="button"
        key={level}
        ref={isSelected ? selectedRef : undefined}
        role="menuitemradio"
        aria-checked={isSelected}
        onClick={() => onSelect(level)}
        onKeyDown={moveFocus}
        className={`w-full flex items-center justify-between px-3 py-1.5 text-[11.5px] text-left hover:bg-panel2 focus-visible:bg-panel2 focus:outline-none cursor-pointer transition select-none ${
          isSelected ? "text-accent font-medium bg-panel2/40" : "text-txt/90"
        }`}
      >
        <span>{label}</span>
        {isSelected && <Check size={11} className="shrink-0 ml-2" />}
      </button>
    );
  });
}
