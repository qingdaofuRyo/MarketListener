import type { BarSeriesOption, LineSeriesOption } from "echarts";
import { isFiniteQuote } from "./marketQuote";

export interface MeasureBar { volume?: number | null; amount?: number | null; openInterest?: number | null }
export interface MeasureEvidence { fieldCapabilities?: Record<string, boolean>; openInterest?: number | null }
export type SecondaryMetric = "openInterest" | "amount";
export const VOLUME_OPACITY = 0.8;

export function resolveSecondaryMetric(evidence: MeasureEvidence | undefined, bars: readonly MeasureBar[], confirmed = false): SecondaryMetric {
  return confirmed || evidence?.fieldCapabilities?.openInterest === true || isFiniteQuote(evidence?.openInterest) ||
    bars.some(bar => isFiniteQuote(bar.openInterest)) ? "openInterest" : "amount";
}

/** Separate axis bindings calculate each extent from its own visible series. */
export function subchartSeries(bars: readonly MeasureBar[], metric: SecondaryMetric, volumeColors: readonly string[], lineColor: string): [BarSeriesOption, LineSeriesOption] {
  return [
    {
      id: "market-volume", name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
      barWidth: "57%", z: 2, animation: false, emphasis: { disabled: true },
      itemStyle: { opacity: VOLUME_OPACITY },
      data: bars.map((bar, index) => ({ value: isFiniteQuote(bar.volume) ? bar.volume : null, itemStyle: { color: volumeColors[index] } })),
    },
    {
      id: "market-secondary", name: metric === "openInterest" ? "持仓量" : "成交额",
      type: "line", xAxisIndex: 1, yAxisIndex: 2, z: 3, animation: false,
      showSymbol: false, connectNulls: false, smooth: false,
      lineStyle: { width: 1.5, color: lineColor }, itemStyle: { color: lineColor }, emphasis: { disabled: true },
      data: bars.map(bar => isFiniteQuote(bar[metric]) ? bar[metric] : null),
    },
  ];
}
