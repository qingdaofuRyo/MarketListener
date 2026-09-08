import { marketSortValue, type SortableMarketInstrument } from "./marketList";

export type QuoteTone = "up" | "down" | "flat" | "missing";
export const isFiniteQuote = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

export function changeTone(value: unknown): QuoteTone {
  return !isFiniteQuote(value) ? "missing" : value > 0 ? "up" : value < 0 ? "down" : "flat";
}

export function formatPercent(value: unknown): string {
  if (!isFiniteQuote(value)) return "—";
  return `${value > 0 ? "+" : ""}${Object.is(value, -0) ? "0.00" : value.toFixed(2)}%`;
}

export function formatQuote(value: unknown, digits = 2, unit = false): string {
  if (!isFiniteQuote(value)) return "—";
  const scale = unit && Math.abs(value) >= 1e8 ? 1e8 : unit && Math.abs(value) >= 1e4 ? 1e4 : 1;
  return (value / scale).toLocaleString("zh-CN", { maximumFractionDigits: digits }) +
    (scale === 1e8 ? "亿" : scale === 1e4 ? "万" : "");
}

export function marketFieldTone(item: SortableMarketInstrument, field: string): QuoteTone {
  if (field === "name" || field === "symbol") return "flat";
  const value = marketSortValue(item, field);
  if (!isFiniteQuote(value)) return "missing";
  if (field === "latestPrice") return changeTone(item.pctChange);
  if (field === "pctChange" || field.startsWith("return")) return changeTone(value);
  return "flat";
}

export function formatMarketField(item: SortableMarketInstrument, field: string): string {
  if (field === "name") return item.name || "—";
  if (field === "symbol") return item.symbol || item.instrumentId;
  const value = marketSortValue(item, field);
  if (field === "pctChange" || field === "amplitude" || field.startsWith("return")) return formatPercent(value);
  return formatQuote(value, field === "latestPrice" ? 4 : 2,
    ["totalMarketCap", "floatMarketCap", "capitalDeposit", "volume", "amount", "openInterest"].includes(field));
}
