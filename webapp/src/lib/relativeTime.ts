/**
 * Compact age for a sidebar row: "now", "5m", "3h", "2d", "3w", then a
 * short date ("Mar 4", or "Mar 4 2025" in another year). Input is epoch
 * seconds, as the server sends it. Unknown or future times give "".
 */
export function compactAge(epochSeconds: number | null | undefined, nowMs: number = Date.now()): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds) || epochSeconds <= 0) return "";
  const thenMs = epochSeconds * 1000;
  const sec = Math.floor((nowMs - thenMs) / 1000);
  if (sec < -60) return "";
  if (sec < 60) return "now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d`;
  if (day < 28) return `${Math.floor(day / 7)}w`;
  const then = new Date(thenMs);
  const sameYear = then.getFullYear() === new Date(nowMs).getFullYear();
  const md = then.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return sameYear ? md : `${md} ${then.getFullYear()}`;
}
