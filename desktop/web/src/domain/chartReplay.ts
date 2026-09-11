export type ReplayState = "closed" | "selecting" | "paused" | "playing" | "ended";
export interface ChartRange { start: number; end: number }
export const REPLAY_SPEEDS = [0.5, 1, 2, 4, 8, 10] as const;
export const REPLAY_EMPTY_SLOTS = 5;

/** Empty slots are axis positions only, never synthetic dates or market bars. */
export function replayRange(count: number, visible: number): ChartRange {
  const end = Math.max(Math.max(0, count - 1) + REPLAY_EMPTY_SLOTS, visible - 1);
  return { start: Math.max(0, end - Math.max(6, visible) + 1), end };
}
export function advanceReplay(count: number, total: number, step = 1): number {
  return Math.min(total, Math.max(0, count) + Math.max(0, step));
}
