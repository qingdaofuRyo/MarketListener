/** Layout and geometry shared by the overview and instrument K-line canvases. */

export const CHART_LAYOUT = {
  overlayGap: 4,
  paneGap: 18,
  axisPadding: 4,
  axisLabelMargin: 3,
} as const;

/** Reserve only measured labels plus their gap; never clip large/negative prices. */
export function chartAxisGutter(labels: readonly string[], measure: (text: string) => number): number {
  return Math.ceil(Math.max(0, ...labels.map(measure))) + CHART_LAYOUT.axisPadding + CHART_LAYOUT.axisLabelMargin;
}
