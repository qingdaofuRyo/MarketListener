import type { FuturesPairedBubblePayload } from "./futures";

export interface TurnoverAxis {
  mode: "quantile" | "linear";
  referenceDay: string | null;
  sampleCount: number;
  anchors: Array<{ amount: number; position: number }>;
}

/** Freeze the last observed day's distribution for the entire playback range. */
export function turnoverAxis(payload: FuturesPairedBubblePayload): TurnoverAxis {
  for (let index = payload.dates.length - 1; index >= 0; index--) {
    const samples = payload.series.map(item => item.horizontalValues[index])
      .filter((value): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0)
      .sort((a, b) => a - b);
    if (!samples.length) continue;
    const distinct = [...new Set(samples)];
    if (samples.length < 3 || distinct.length < 3)
      return { mode: "linear", referenceDay: payload.dates[index], sampleCount: samples.length, anchors: [] };
    const anchors = [{ amount: 0, position: 0 }];
    let begin = 0;
    while (begin < samples.length) {
      let end = begin + 1;
      while (end < samples.length && samples[end] === samples[begin]) end++;
      if (samples[begin] > 0)
        anchors.push({ amount: samples[begin], position: (begin + (end - begin) / 2) / samples.length * 100 });
      begin = end;
    }
    return { mode: "quantile", referenceDay: payload.dates[index], sampleCount: samples.length, anchors };
  }
  return { mode: "linear", referenceDay: null, sampleCount: 0, anchors: [] };
}

function interpolate(value: number, anchors: Array<{ amount: number; position: number }>, reverse: boolean): number {
  const input = (anchor: { amount: number; position: number }) => reverse ? anchor.position : anchor.amount;
  const output = (anchor: { amount: number; position: number }) => reverse ? anchor.amount : anchor.position;
  let right = 1;
  while (right < anchors.length - 1 && input(anchors[right]) < value) right++;
  const before = anchors[right - 1], after = anchors[right];
  if (right === anchors.length - 1 && value > input(after)) {
    const width = after.amount - before.amount, step = after.position - before.position;
    return reverse ? after.amount + width * Math.expm1((value - after.position) / step)
      : after.position + step * Math.log1p((value - after.amount) / width);
  }
  return output(before) + (value - input(before)) / (input(after) - input(before)) * (output(after) - output(before));
}

export function turnoverPosition(axis: TurnoverAxis, amount: number): number {
  if (axis.mode === "linear") return amount;
  return interpolate(Math.max(0, amount), axis.anchors, false);
}

export function turnoverAmount(axis: TurnoverAxis, position: number): number {
  if (axis.mode === "linear") return position;
  return interpolate(position, axis.anchors, true);
}
