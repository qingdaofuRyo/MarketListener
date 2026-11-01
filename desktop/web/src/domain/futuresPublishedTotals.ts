import type { RankingSeries } from "./futures";

/** Sum published member values for each trading day; all-missing stays unknown. */
export function sumPublishedValues(rows: readonly RankingSeries[], dates: readonly string[]): Array<number | null> {
  return dates.map((_, day) => {
    let total = 0;
    let published = false;
    for (const row of rows) {
      const value = row.values[day];
      if (typeof value !== "number" || !Number.isFinite(value)) continue;
      total += value;
      published = true;
    }
    return published ? total : null;
  });
}
