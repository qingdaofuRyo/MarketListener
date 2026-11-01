export type SpreadSide = "long" | "short";
export interface SpreadRegion { side: SpreadSide; points: Array<[number, number]> }

function valid(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Build colored polygons between two published position series, splitting at crossings. */
export function buildSpreadRegions(longValues: readonly (number | null)[], shortValues: readonly (number | null)[]): SpreadRegion[] {
  const regions: SpreadRegion[] = [];
  const length = Math.min(longValues.length, shortValues.length);
  for (let index = 0; index + 1 < length; index++) {
    const longStart = longValues[index], longEnd = longValues[index + 1];
    const shortStart = shortValues[index], shortEnd = shortValues[index + 1];
    if (!valid(longStart) || !valid(longEnd) || !valid(shortStart) || !valid(shortEnd)) continue;
    const before = longStart - shortStart, after = longEnd - shortEnd;
    if (before === 0 && after === 0) continue;
    if (before >= 0 && after >= 0) {
      regions.push({ side: "long", points: [[index, longStart], [index + 1, longEnd], [index + 1, shortEnd], [index, shortStart]] });
    } else if (before <= 0 && after <= 0) {
      regions.push({ side: "short", points: [[index, longStart], [index + 1, longEnd], [index + 1, shortEnd], [index, shortStart]] });
    } else {
      const fraction = Math.abs(before) / (Math.abs(before) + Math.abs(after));
      const crossing: [number, number] = [index + fraction, longStart + fraction * (longEnd - longStart)];
      regions.push({ side: before > 0 ? "long" : "short", points: [[index, longStart], crossing, [index, shortStart]] });
      regions.push({ side: after > 0 ? "long" : "short", points: [crossing, [index + 1, longEnd], [index + 1, shortEnd]] });
    }
  }
  return regions;
}
