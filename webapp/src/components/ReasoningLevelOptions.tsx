import type { RefObject } from "react";
import { Menu } from "@base-ui/react/menu";
import { Check } from "lucide-react";
import type { ReasoningEffort } from "../lib/api";
import { REASONING_LEVELS } from "../lib/reasoningSupport";

/** Reasoning-level rows inside a `MenuPanel`. Pass ``selectedRef`` and focus it
 * from `onOpenChangeComplete` so the keyboard starts on the current level. */
export default function ReasoningLevelOptions({
  value,
  onSelect,
  selectedRef,
}: {
  value: ReasoningEffort;
  onSelect: (level: ReasoningEffort) => void;
  selectedRef?: RefObject<HTMLDivElement | null>;
}) {
  return (
    <Menu.RadioGroup
      aria-label="Reasoning level"
      value={value}
      onValueChange={(level: ReasoningEffort) => onSelect(level)}
    >
      {REASONING_LEVELS.map(({ value: level, label }) => {
        const isSelected = level === value;
        return (
          <Menu.RadioItem
            key={level}
            value={level}
            closeOnClick
            ref={isSelected ? selectedRef : undefined}
            className={`w-full flex items-center justify-between px-3 py-1.5 text-ui-11.5 text-left outline-none cursor-pointer transition-colors select-none data-[highlighted]:bg-panel2 ${
              isSelected ? "text-accent font-medium bg-panel2/40" : "text-txt/90"
            }`}
          >
            <span>{label}</span>
            <Menu.RadioItemIndicator className="shrink-0 ml-2">
              <Check size={11} />
            </Menu.RadioItemIndicator>
          </Menu.RadioItem>
        );
      })}
    </Menu.RadioGroup>
  );
}
