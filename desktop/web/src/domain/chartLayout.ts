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
