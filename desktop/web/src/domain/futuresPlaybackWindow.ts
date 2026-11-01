export type FuturesPlaybackWindow = "20d" | "60d" | "6m" | "3y";

export const futuresPlaybackWindows: Array<{ value: FuturesPlaybackWindow; label: string }> = [
  { value: "20d", label: "近20交易日" },
  { value: "60d", label: "近60交易日" },
  { value: "6m", label: "近半年交易日" },
  { value: "3y", label: "近3年交易日" },
];

function monthsEarlier(day: string, count: number): string {
  const [year, month, date] = day.split("-").map(Number);
  const start = new Date(Date.UTC(year, month - 1 - count, 1));
  const monthEnd = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0)).getUTCDate();
  start.setUTCDate(Math.min(date, monthEnd));
  return start.toISOString().slice(0, 10);
}

export function firstPlaybackIndex(dates: readonly string[], window: FuturesPlaybackWindow): number {
  if (!dates.length) return 0;
  if (window === "20d" || window === "60d") return Math.max(0, dates.length - (window === "20d" ? 20 : 60));
  const cutoff = monthsEarlier(dates[dates.length - 1], window === "6m" ? 6 : 36);
  const first = dates.findIndex(day => day >= cutoff);
  return first < 0 ? 0 : first;
}
