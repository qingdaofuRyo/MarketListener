import type { KLineBar } from "../components/charts/KLineChart.vue";

export type ChartType = "candles" | "hollow" | "heikin" | "line" | "area";
export const chartTypes: Array<{ value: ChartType; label: string }> = [
  { value: "candles", label: "实心K线图" },
  { value: "hollow", label: "空心K线图" },
  { value: "heikin", label: "平均K线图" },
  { value: "line", label: "折线图" },
  { value: "area", label: "面积图" },
];

export const chartTypeGroups = [
  { label: "K线", values: ["candles", "hollow"] },
  { label: "线与面积", values: ["line", "area"] },
  { label: "派生显示", values: ["heikin"] },
].map(group => ({ label: group.label, items: group.values.map(value => chartTypes.find(item => item.value === value)!) }));

/** Heikin Ashi is a presentation transform; missing bars reset the seed. */
export function heikinAshi(bars: KLineBar[]): KLineBar[] {
  let previous: { open: number; close: number } | undefined;
  return bars.map((bar) => {
    const { open, high, low, close } = bar;
    if (![open, high, low, close].every((v) => typeof v === "number" && Number.isFinite(v))) {
      previous = undefined;
      return { ...bar, open: undefined, high: undefined, low: undefined, close: undefined };
    }
    const nextClose = (open! + high! + low! + close!) / 4;
    const nextOpen = previous ? (previous.open + previous.close) / 2 : (open! + close!) / 2;
    previous = { open: nextOpen, close: nextClose };
    return { ...bar, open: nextOpen, close: nextClose, high: Math.max(high!, nextOpen, nextClose), low: Math.min(low!, nextOpen, nextClose) };
  });
}
