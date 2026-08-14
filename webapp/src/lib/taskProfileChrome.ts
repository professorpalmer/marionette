/**
 * Live task-depth chip. Kernel emits ConvEvent("task_profile"); the webapp
 * had no handler. StatusBar subscribes so MICRO/STANDARD/DEEP is visible.
 */

export type TaskProfileChip = {
  profile: string;
  source?: string;
  escalated_from?: string | null;
};

export const HARNESS_TASK_PROFILE = "harness-task-profile";

export function normalizeTaskProfile(raw: unknown): string {
  const profile = String(raw || "").trim().toUpperCase();
  if (profile === "MICRO" || profile === "STANDARD" || profile === "DEEP") {
    return profile;
  }
  return "";
}

export function taskProfileTitle(chip: TaskProfileChip): string {
  const profile = normalizeTaskProfile(chip.profile);
  const source = (chip.source || "").trim();
  const escalated = (chip.escalated_from || "").trim();
  const parts = [profile || "task profile"];
  if (source) parts.push(`source ${source}`);
  if (escalated) parts.push(`escalated from ${escalated}`);
  if (profile === "MICRO") parts.push("skipped wiki and CodeGraph auto-inject");
  return parts.join(" — ");
}

export function publishTaskProfile(chip: TaskProfileChip): void {
  const profile = normalizeTaskProfile(chip.profile);
  if (!profile || typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent(HARNESS_TASK_PROFILE, {
      detail: { ...chip, profile },
    }),
  );
}

export function subscribeTaskProfile(
  handler: (chip: TaskProfileChip) => void,
): () => void {
  if (typeof window === "undefined") return () => {};
  const onEvent = (event: Event) => {
    const detail = (event as CustomEvent<TaskProfileChip>).detail;
    const profile = normalizeTaskProfile(detail?.profile);
    if (!profile) return;
    handler({ ...detail, profile });
  };
  window.addEventListener(HARNESS_TASK_PROFILE, onEvent);
  return () => window.removeEventListener(HARNESS_TASK_PROFILE, onEvent);
}
