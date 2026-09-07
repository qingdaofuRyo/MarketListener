/** Layout and geometry shared by the overview and instrument K-line canvases. */

export const CHART_LAYOUT = {
  quoteSlot: 65,
  overlayGap: 4,
  boardGap: 12,
  paneGap: 14,
  secondaryBackRatio: 0.95,
  secondaryFrontRatio: 0.6,
} as const;

export interface NestedBarGeometry {
  center: number;
  back: { x: number; width: number };
  front: { x: number; width: number };
}

/**
 * Two measures use different axes but one time-slot.  Keeping the computation
 * outside ECharts makes the center/visible-side invariant testable.
 */
export function nestedBarGeometry(
  center: number,
  slotWidth: number,
  backRatio = CHART_LAYOUT.secondaryBackRatio,
  frontRatio = CHART_LAYOUT.secondaryFrontRatio,
): NestedBarGeometry {
  const usable = Math.max(0, slotWidth);
  const backWidth = usable * Math.max(0, Math.min(1, backRatio));
  const frontWidth = backWidth * Math.max(0, Math.min(1, frontRatio));
  return {
    center,
    back: { x: center - backWidth / 2, width: backWidth },
    front: { x: center - frontWidth / 2, width: frontWidth },
  };
}

export function finiteExtent(values: readonly (number | null | undefined)[]): {
  min: number;
  max: number;
} | null {
  const finite = values.filter((value): value is number =>
    typeof value === "number" && Number.isFinite(value),
  );
  if (!finite.length) return null;
  return { min: Math.min(0, ...finite), max: Math.max(0, ...finite) };
}
