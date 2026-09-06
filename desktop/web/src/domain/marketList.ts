/** Shared market-list semantics for the overview and detail side list. */

export const DEFAULT_MARKET_CATEGORY = "cn-future-main";
export type MarketSortDirection = "asc" | "desc" | null;

export interface MarketSortState {
  field: string | null;
  direction: MarketSortDirection;
}

export interface SortableMarketInstrument {
  instrumentId: string;
  symbol?: string;
  name?: string;
  latestPrice?: number | null;
  lastClose?: number | null;
  totalMarketCap?: number | null;
  floatMarketCap?: number | null;
  capitalDeposit?: number | null;
  openInterest?: number | null;
  pctChange?: number | null;
  amplitude?: number | null;
  volume?: number | null;
  amount?: number | null;
  dailyReturns?: Record<string, number | null>;
}

export function nextSortState(
  current: MarketSortState,
  field: string,
): MarketSortState {
  if (current.field !== field) return { field, direction: "asc" };
  if (current.direction === "asc") return { field, direction: "desc" };
  return { field: null, direction: null };
}

export function marketSortValue(
  item: SortableMarketInstrument,
  field: string,
): string | number | null {
  if (field === "name") return item.name || item.symbol || item.instrumentId;
  if (field === "symbol") return item.symbol || item.instrumentId;
  if (field.startsWith("return")) return item.dailyReturns?.[field.slice(6)] ?? null;
  const value = item[field as keyof SortableMarketInstrument];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function sortMarketInstruments<T extends SortableMarketInstrument>(
  items: readonly T[],
  state: MarketSortState,
): T[] {
  if (!state.field || !state.direction) return [...items];
  const factor = state.direction === "asc" ? 1 : -1;
  return items
    .map((item, index) => ({ item, index, value: marketSortValue(item, state.field!) }))
    .sort((left, right) => {
      const leftMissing = left.value == null;
      const rightMissing = right.value == null;
      if (leftMissing || rightMissing) {
        if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
      } else if (typeof left.value === "number" && typeof right.value === "number") {
        if (left.value !== right.value) return (left.value - right.value) * factor;
      } else {
        const compare = String(left.value).localeCompare(String(right.value), "zh-CN");
        if (compare) return compare * factor;
      }
      return left.index - right.index || left.item.instrumentId.localeCompare(right.item.instrumentId);
    })
    .map(({ item }) => item);
}

export function weekdayLabel(isoDay: string): string {
  const day = isoDay.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    weekday: "short",
    timeZone: "Asia/Shanghai",
  }).format(new Date(`${day}T00:00:00+08:00`));
}

export function editableTarget(target: EventTarget | null): boolean {
  const element = target instanceof HTMLElement ? target : null;
  return Boolean(
    element?.closest("input, textarea, select, [contenteditable='true'], [role='textbox']"),
  );
}
