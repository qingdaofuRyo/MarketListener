/** A fixed, invertible signed scale spreads small moves without clipping extremes. */
export const DIRECTION_AXIS_REFERENCE_PP = 10;

export function directionPosition(value: number): number {
  return Math.asinh(value / DIRECTION_AXIS_REFERENCE_PP);
}

export function directionAmount(position: number): number {
  return DIRECTION_AXIS_REFERENCE_PP * Math.sinh(position);
}
