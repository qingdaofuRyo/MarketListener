import type { FuturesMemberPositionHistoryResponse, FuturesMemberPositionHistoryRow } from "./futures";

export interface PositionPieSlice { key: string; name: string; value: number }
export interface PositionTrendSeries {
  key: string;
  name: string;
  values: Array<number | null>;
  changes: Array<number | null>;
  details?: Array<{
    longPosition: number | null;
    shortPosition: number | null;
    longChange: number | null;
    shortChange: number | null;
    exchanges: string[];
  } | null>;
}
export interface PositionChartData {
  pie: PositionPieSlice[];
  trend: PositionTrendSeries[];
  changeTotals: Array<number | null>;
}

export type PositionMetric = "gross" | "longPosition" | "shortPosition" | "netLongPosition" | "netShortPosition";
type Observation = { position: number; change: number | null };
type Accumulated = { position: number; change: number; changeComplete: boolean };

function metricObservation(row: FuturesMemberPositionHistoryRow, metric: PositionMetric): Observation | null {
  if (metric === "gross") {
    if (row.longPosition === null && row.shortPosition === null) return null;
    const changes = [
      ...(row.longPosition !== null ? [row.longPositionChange] : []),
      ...(row.shortPosition !== null ? [row.shortPositionChange] : []),
    ];
    return {
      position: (row.longPosition ?? 0) + (row.shortPosition ?? 0),
      change: changes.every(value => value !== null)
        ? changes.reduce<number>((sum, value) => sum + Number(value), 0) : null,
    };
  }
  const position = row[metric];
  if (position === null) return null;
  const change = row[`${metric}Change` as keyof FuturesMemberPositionHistoryRow];
  return { position, change: typeof change === "number" ? change : null };
}

function buildCharts(
  history: FuturesMemberPositionHistoryResponse,
  selectedDay: string,
  metric: PositionMetric,
  dimension: (row: FuturesMemberPositionHistoryRow) => { key: string; name: string },
): PositionChartData {
  const names = new Map<string, string>();
  const byKey = new Map<string, Map<string, Accumulated>>();
  for (const row of history.rows) {
    const observation = metricObservation(row, metric);
    if (observation === null || !Number.isFinite(observation.position) || observation.position < 0) continue;
    const { key, name } = dimension(row);
    names.set(key, name);
    const byDay = byKey.get(key) ?? new Map<string, Accumulated>();
    const previous = byDay.get(row.tradingDay) ?? { position: 0, change: 0, changeComplete: true };
    previous.position += observation.position;
    if (observation.change === null || !Number.isFinite(observation.change)) {
      previous.changeComplete = false;
    } else {
      previous.change += observation.change;
    }
    byDay.set(row.tradingDay, previous);
    byKey.set(key, byDay);
  }
  const keys = [...byKey.keys()].sort((left, right) => {
    const leftDay = byKey.get(left)?.get(selectedDay)?.position ?? -1;
    const rightDay = byKey.get(right)?.get(selectedDay)?.position ?? -1;
    return rightDay - leftDay || left.localeCompare(right);
  });
  return {
    pie: keys.flatMap((key) => {
      const value = byKey.get(key)?.get(selectedDay)?.position;
      return value !== undefined && value > 0
        ? [{ key, name: names.get(key) ?? key, value }]
        : [];
    }),
    trend: keys.map(key => ({
      key,
      name: names.get(key) ?? key,
      values: history.dates.map(day => byKey.get(key)?.get(day)?.position ?? null),
      changes: history.dates.map(day => {
        const item = byKey.get(key)?.get(day);
        return item?.changeComplete ? item.change : null;
      }),
    })),
    changeTotals: history.dates.map(day => {
      const items = keys.flatMap(key => {
        const item = byKey.get(key)?.get(day);
        return item === undefined ? [] : [item];
      });
      return items.length && items.every(item => item.changeComplete)
        ? items.reduce((sum, item) => sum + item.change, 0) : null;
    }),
  };
}

export function memberCommodityCharts(
  history: FuturesMemberPositionHistoryResponse,
  selectedDay: string,
): PositionChartData {
  const chart = buildCharts(history, selectedDay, "gross", row => ({
    key: row.productKey,
    name: row.productDisplayName,
  }));
  const details = new Map<string, Map<string, {
    longPosition: number; shortPosition: number;
    longCount: number; shortCount: number;
    longChange: number; shortChange: number;
    longChangeComplete: boolean; shortChangeComplete: boolean;
    exchanges: Set<string>;
  }>>();
  for (const row of history.rows) {
    const byDay = details.get(row.productKey) ?? new Map();
    const current = byDay.get(row.tradingDay) ?? {
      longPosition: 0, shortPosition: 0, longCount: 0, shortCount: 0,
      longChange: 0, shortChange: 0, longChangeComplete: true,
      shortChangeComplete: true, exchanges: new Set<string>(),
    };
    current.exchanges.add(row.exchange);
    if (row.longPosition !== null) {
      current.longPosition += row.longPosition;
      current.longCount += 1;
      if (row.longPositionChange === null) current.longChangeComplete = false;
      else current.longChange += row.longPositionChange;
    }
    if (row.shortPosition !== null) {
      current.shortPosition += row.shortPosition;
      current.shortCount += 1;
      if (row.shortPositionChange === null) current.shortChangeComplete = false;
      else current.shortChange += row.shortPositionChange;
    }
    byDay.set(row.tradingDay, current);
    details.set(row.productKey, byDay);
  }
  return {
    ...chart,
    trend: chart.trend.map(series => ({
      ...series,
      details: history.dates.map(day => {
        const item = details.get(series.key)?.get(day);
        return item ? {
          longPosition: item.longCount ? item.longPosition : null,
          shortPosition: item.shortCount ? item.shortPosition : null,
          longChange: item.longCount && item.longChangeComplete ? item.longChange : null,
          shortChange: item.shortCount && item.shortChangeComplete ? item.shortChange : null,
          exchanges: [...item.exchanges].sort(),
        } : null;
      }),
    })),
  };
}

export function commodityMemberCharts(
  history: FuturesMemberPositionHistoryResponse,
  selectedDay: string,
  metric: Exclude<PositionMetric, "gross">,
): PositionChartData {
  return buildCharts(history, selectedDay, metric, row => ({
    key: row.memberKey,
    name: row.memberName,
  }));
}
