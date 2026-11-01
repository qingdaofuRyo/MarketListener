/** Layout and geometry shared by the overview and instrument K-line canvases. */

export const CHART_LAYOUT = {
  overlayGap: 4,
  paneGap: 18,
  axisPadding: 2,
  axisLabelMargin: 3,
} as const;

export const CHART_BARS = {
  candle: { barWidth: "80%", barMaxWidth: 24 },
  volume: { barWidth: "90%", barMaxWidth: 28 },
} as const;

/** One explicit domain/tick set is used both for labels and their layout budget. */
export function chartAxisScale(minimum: number, maximum: number, splits = 5) {
  const span = maximum - minimum || Math.max(Math.abs(maximum), 1);
  const raw = span / splits;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalized = raw / magnitude;
  const interval = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
  const min = Math.min(minimum, Number((Math.floor(minimum / interval) * interval).toPrecision(14)));
  const max = Math.max(maximum, Number((Math.ceil((maximum === minimum ? maximum + span : maximum) / interval) * interval).toPrecision(14)));
  const ticks = Array.from({length: Math.round((max-min)/interval)+1}, (_,i)=>Number((min+i*interval).toPrecision(14)));
  return { min, max, interval, ticks };
}

/** Reserve only measured labels plus their gap; never clip large/negative prices. */
export function chartAxisGutter(labels: readonly string[], measure: (text: string) => number): number {
  return Math.ceil(Math.max(0, ...labels.map(measure))) + CHART_LAYOUT.axisPadding + CHART_LAYOUT.axisLabelMargin;
}

/** Compare every bar identity, not just endpoints; revised prices may keep the view. */
export function sameBarTimeline(
  next: readonly { barOpenTime?: string; tradingDate?: string }[],
  previous: readonly { barOpenTime?: string; tradingDate?: string }[],
): boolean {
  return next.length > 0 && next.length === previous.length && next.every((bar, index) => {
    const key = bar.barOpenTime || bar.tradingDate;
    const oldKey = previous[index].barOpenTime || previous[index].tradingDate;
    return typeof key === "string" && key.length > 0 && key === oldKey;
  });
}

type TimedBar = { barOpenTime?: string; tradingDate?: string };
type VisibleRange = { start: number; end: number };
const barTime = (bar: TimedBar): string | undefined => bar.barOpenTime || bar.tradingDate;

/** Keep a visible time window when a new bar rolls the loaded window forward. */
export function remapBarRange(next: readonly TimedBar[], previous: readonly TimedBar[], view: VisibleRange): VisibleRange | null {
  if (!next.length || !previous.length || view.start < 0 || view.end < view.start || view.end >= previous.length)
    return null;
  if (sameBarTimeline(next, previous)) return { ...view };
  const times = next.map(barTime);
  if (times.some(value => !value) || new Set(times).size !== times.length) return null;
  const positions = new Map(times.map((value, index) => [value, index]));
  const visible = previous.slice(view.start, view.end + 1).map(barTime);
  if (visible.some(value => !value) || new Set(visible).size !== visible.length) return null;
  const mapped = visible.map(value => positions.get(value));
  const present = mapped.filter((value): value is number => value !== undefined);
  if (mapped.slice(1).some(value => value === undefined)) return null;
  if (present.length < Math.max(1, visible.length - 1)
      || present.some((value, index) => index > 0 && value !== present[index - 1] + 1)) return null;
  const oldLatest = barTime(previous.at(-1)!);
  const lastOldIndex = oldLatest ? positions.get(oldLatest) : undefined;
  if (view.end === previous.length - 1 && oldLatest && times.at(-1)! < oldLatest) return null;
  if (view.end === previous.length - 1 && lastOldIndex !== undefined
      && lastOldIndex >= next.length - 2 && times.at(-1)! >= oldLatest!) {
    const end = next.length - 1;
    return { start: Math.max(0, end - visible.length + 1), end };
  }
  if (present.length !== visible.length) return null;
  return { start: present[0], end: present.at(-1)! };
}

/** Reuse earlier bars only when the refreshed tail overlaps almost entirely. */
export function mergeBarTail<T extends TimedBar>(previous: readonly T[], fresh: readonly T[]): T[] | null {
  if (previous.length <= fresh.length || !fresh.length) return null;
  const previousTimes = previous.map(barTime), freshTimes = fresh.map(barTime);
  if (previousTimes.some(value => !value) || freshTimes.some(value => !value)
      || new Set(previousTimes).size !== previousTimes.length || new Set(freshTimes).size !== freshTimes.length)
    return null;
  const overlapStart = previousTimes.indexOf(freshTimes[0]);
  if (overlapStart < 0) return null;
  const overlap = previous.length - overlapStart;
  if (overlap < Math.max(1, fresh.length - 2) || overlap > fresh.length) return null;
  if (previousTimes.slice(overlapStart).some((value, index) => value !== freshTimes[index])) return null;
  return [...previous.slice(0, overlapStart), ...fresh];
}

/** A history page must precede the current first bar and advance the cursor. */
export function prependHistoryPage<T extends TimedBar>(older: readonly T[], current: readonly T[]): T[] | null {
  if (!older.length || !current.length) return null;
  const olderTimes = older.map(barTime), currentTimes = current.map(barTime);
  if ([...olderTimes, ...currentTimes].some(value => !value)) return null;
  const keys = [...olderTimes, ...currentTimes] as string[];
  if (new Set(keys).size !== keys.length) return null;
  const moments = keys.map(value => Date.parse(value));
  if (moments.some(value => !Number.isFinite(value))
      || moments.some((value, index) => index > 0 && value <= moments[index - 1])) return null;
  return [...older, ...current];
}
