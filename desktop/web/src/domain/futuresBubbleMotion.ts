import type { FuturesPairedBubbleSeries } from "./futures";

export interface BubbleFrame {
  x: number;
  y: number;
  size: number;
  source: {
    x: number | null; y: number | null; size: number | null;
    dailyReturn: number | null; tenDayReturn: number | null;
    shortMove3: number | null; shortRank: number | null; shortSampleCount: number | null; shortCompareStatus?: string;
    directionUpCount: number | null; directionDownCount: number | null; directionFlatCount: number | null;
    directionWindowCount: number | null; directionStatus?: string; directionReason?: string;
    coverage?: number; windowMin?: number; windowMax?: number; shortContracts?: number;
    depositReason?: string; missingMarginContracts?: number; marginPublicationDay?: string;
  };
  heldXDay?: string;
  heldYDay?: string;
  heldSizeDay?: string;
  missing: boolean;
}

const MAX_DEPOSIT_BUBBLE_DIAMETER = 72;
const MIN_DEPOSIT_BUBBLE_DIAMETER = 12;
const DEPOSIT_LOG_REFERENCE_YI = 20;

/** A logarithmic visual scale separates small deposits without swelling all bubbles. */
export function depositBubbleDiameter(size: number, scale: number): number {
  if (!Number.isFinite(size) || size < 0 || !Number.isFinite(scale) || scale <= 0)
    return MIN_DEPOSIT_BUBBLE_DIAMETER;
  return MIN_DEPOSIT_BUBBLE_DIAMETER
    + (MAX_DEPOSIT_BUBBLE_DIAMETER - MIN_DEPOSIT_BUBBLE_DIAMETER)
    * Math.log1p(Math.min(size, scale) / DEPOSIT_LOG_REFERENCE_YI)
    / Math.log1p(scale / DEPOSIT_LOG_REFERENCE_YI);
}

function finite(value: number | null | undefined, nonnegative = false): number | null {
  return typeof value === "number" && Number.isFinite(value) && (!nonnegative || value >= 0) ? value : null;
}

/** Keep a product on the canvas after its first published metric observation.
 * Missing values remain null in source; carried coordinates are display-only.
 */
export function resolveBubbleFrames(item: FuturesPairedBubbleSeries, dates: string[]): Array<BubbleFrame | null> {
  let lastX: { value: number; day: string } | undefined;
  let lastY: { value: number; day: string } | undefined;
  let lastSize: { value: number; day: string } | undefined;
  return dates.map((day, index) => {
    const x = finite(item.horizontalValues[index], true);
    const y = finite(item.verticalValues[index]);
    const size = finite(item.sizeValues[index], true);
    if (x !== null) lastX = { value: x, day };
    if (y !== null) lastY = { value: y, day };
    if (size !== null) lastSize = { value: size, day };
    if (!lastX && !lastY && !lastSize) return null;
    return {
      x: lastX?.value ?? 0, y: lastY?.value ?? 0, size: lastSize?.value ?? 0,
      source: {
        x, y, size,
        dailyReturn: finite(item.dailyReturnValues?.[index]),
        tenDayReturn: finite(item.tenDayReturnValues?.[index]),
        shortMove3: finite(item.shortMove3Values?.[index]),
        shortRank: finite(item.shortRankValues?.[index], true),
        shortSampleCount: finite(item.shortSampleCountValues?.[index], true),
        shortCompareStatus: item.shortCompareStatusValues?.[index] || undefined,
        directionUpCount: finite(item.directionUpCountValues?.[index], true),
        directionDownCount: finite(item.directionDownCountValues?.[index], true),
        directionFlatCount: finite(item.directionFlatCountValues?.[index], true),
        directionWindowCount: finite(item.directionWindowCountValues?.[index], true),
        directionStatus: item.directionStatusValues?.[index] || undefined,
        directionReason: item.directionReasonValues?.[index] || undefined,
        coverage: finite(item.coverageValues?.[index]) ?? undefined,
        windowMin: finite(item.windowMinValues?.[index]) ?? undefined,
        windowMax: finite(item.windowMaxValues?.[index]) ?? undefined,
        shortContracts: finite(item.shortContractCountValues?.[index]) ?? undefined,
        depositReason: item.depositReasonValues?.[index] || undefined,
        missingMarginContracts: finite(item.missingMarginContractCountValues?.[index]) ?? undefined,
        marginPublicationDay: item.marginPublicationDayValues?.[index] || undefined,
      },
      heldXDay: x === null ? lastX?.day : undefined,
      heldYDay: y === null ? lastY?.day : undefined,
      heldSizeDay: size === null ? lastSize?.day : undefined,
      missing: x === null || y === null || size === null,
    };
  });
}

function tangent(before: number, after: number): number {
  return before * after <= 0 ? 0 : 2 * before * after / (before + after);
}

/** Monotone cubic motion passes through official snapshots without overshoot. */
export function fluidBubbleValue(previous: number | undefined, start: number, end: number, following: number | undefined, progress: number): number {
  const delta = end - start;
  const first = previous === undefined ? delta : tangent(start - previous, delta);
  const last = following === undefined ? delta : tangent(delta, following - end);
  const squared = progress * progress, cubed = squared * progress;
  return (2 * cubed - 3 * squared + 1) * start + (cubed - 2 * squared + progress) * first
    + (-2 * cubed + 3 * squared) * end + (cubed - squared) * last;
}

export function fluidBubbleVisibility(progress: number): number {
  return progress * progress * (3 - 2 * progress);
}
