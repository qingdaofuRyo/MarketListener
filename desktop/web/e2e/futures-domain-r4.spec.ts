import { expect, test } from "@playwright/test";
import { bubbleColorsFor, minimumBubbleColorDistance, minimumBubbleTextContrast } from "../src/domain/futuresBubbleColors";
import { depositBubbleDiameter, fluidBubbleValue, fluidBubbleVisibility, resolveBubbleFrames } from "../src/domain/futuresBubbleMotion";
import { buildSpreadRegions } from "../src/domain/futuresSpreadArea";
import { sumPublishedValues } from "../src/domain/futuresPublishedTotals";
import { turnoverAmount, turnoverAxis, turnoverPosition } from "../src/domain/futuresTurnoverAxis";
import type { FuturesPairedBubblePayload } from "../src/domain/futures";

test("turnover quantiles retain real amounts, ties and stable historical positions", () => {
  const payload: FuturesPairedBubblePayload = {
    dates: ["2026-10-26", "2026-10-27"], xLabel: "成交额", yLabel: "涨跌幅", sizeLabel: "资金",
    xUnit: "亿元", yUnit: "%", sizeUnit: "亿元", ySigned: true,
    series: [0, 0, 10, 20, 20, 50, 100, 200, 400, 1000, 4000].map((value, index) => ({
      key: String(index), name: String(index), horizontalValues: [index === 2 ? 10000 : value, value],
      verticalValues: [0, 0], sizeValues: [1, 1],
    })).concat([{ key: "missing", name: "缺报", horizontalValues: [123, null],
      verticalValues: [0, 0], sizeValues: [1, 1] }]),
  };
  const axis = turnoverAxis(payload);
  expect(axis.mode).toBe("quantile");
  expect(axis.referenceDay).toBe("2026-10-27");
  expect(axis.sampleCount).toBe(11);
  expect(turnoverPosition(axis, 0)).toBe(0);
  expect(axis.anchors.filter(anchor => anchor.amount === 20)).toHaveLength(1);
  const values = [0, 10, 20, 50, 100, 200, 400, 1000, 4000, 10000];
  const positions = values.map(value => turnoverPosition(axis, value));
  expect(positions.every((value, index) => index === 0 || value > positions[index - 1])).toBe(true);
  for (const amount of values) expect(turnoverAmount(axis, turnoverPosition(axis, amount))).toBeCloseTo(amount, 8);
  expect(turnoverPosition(axis, 100) - turnoverPosition(axis, 10)).toBeGreaterThan(20);
  expect(turnoverAxis({ ...payload, series: payload.series.slice(0, 2) }).mode).toBe("linear");
  expect(turnoverAxis({ ...payload, series: payload.series.slice(0, 1) }).mode).toBe("linear");
  expect(turnoverAxis({ ...payload, series: [] }).referenceDay).toBeNull();
});

test("limited bubble palette and fluid motion preserve source endpoints", () => {
  const colors = Object.values(bubbleColorsFor(Array.from({ length: 91 }, (_, index) => `test-product-${index}`)));
  expect(new Set(colors).size).toBe(16);
  expect(minimumBubbleColorDistance(colors)).toBeGreaterThan(10);
  expect(minimumBubbleTextContrast(colors)).toBeGreaterThan(6);
  const values = Array.from({ length: 101 }, (_, index) => fluidBubbleValue(0, 2, 8, 5, index / 100));
  expect(values[0]).toBe(2); expect(values.at(-1)).toBe(8);
  expect(values.every((value, index) => value >= 2 && value <= 8 && (!index || value >= values[index - 1]))).toBe(true);
  const epsilon = .00001;
  const before = (8 - fluidBubbleValue(0, 2, 8, 10, 1 - epsilon)) / epsilon;
  const after = (fluidBubbleValue(2, 8, 10, 14, epsilon) - 8) / epsilon;
  expect(Math.abs(before - after)).toBeLessThan(.001);
  expect(fluidBubbleVisibility(0)).toBe(0); expect(fluidBubbleVisibility(1)).toBe(1);
});

test("one-yi and forty-yi deposits remain visually distinct", () => {
  const scale = 1431.19;
  const one = depositBubbleDiameter(1, scale);
  const forty = depositBubbleDiameter(40, scale);
  const hundred = depositBubbleDiameter(100, scale);
  const thousand = depositBubbleDiameter(1000, scale);
  expect(one).toBeGreaterThan(12);
  expect(one).toBeLessThan(14);
  expect(forty - one).toBeGreaterThan(14);
  expect(one).toBeLessThan(forty);
  expect(forty).toBeLessThan(hundred);
  expect(hundred).toBeLessThan(thousand);
  expect(thousand - hundred).toBeGreaterThan(25);
  expect(depositBubbleDiameter(0, scale)).toBe(12);
  expect(depositBubbleDiameter(scale, scale)).toBe(72);
});

test("published product remains visible through missing returns and deposit without inventing either", () => {
  const dates = ["2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"];
  const frames = resolveBubbleFrames({ key: "DCE.M", name: "豆粕", horizontalValues: [null, 100, 120, 140],
    verticalValues: [null, 2, null, 3], sizeValues: [null, 50, null, 60] }, dates);
  expect(frames[0]).toBeNull(); // No published metric yet.
  expect(frames[1]).toMatchObject({ x: 100, y: 2, size: 50, missing: false });
  expect(frames[2]).toMatchObject({ x: 120, y: 2, size: 50, heldYDay: dates[1], heldSizeDay: dates[1],
    missing: true, source: { x: 120, y: null, size: null } });
  expect(frames[3]).toMatchObject({ x: 140, y: 3, size: 60, missing: false });
  const zero = resolveBubbleFrames({ key: "CZCE.JR", name: "粳稻", horizontalValues: [0],
    verticalValues: [0], sizeValues: [0] }, [dates[0]]);
  expect(zero[0]).toMatchObject({ x: 0, size: 0, missing: false, source: { x: 0, y: 0, size: 0 } });
  const noDeposit = resolveBubbleFrames({ key: "DCE.A", name: "豆一", horizontalValues: [null, 100],
    verticalValues: [2, 3], sizeValues: [null, null] }, dates.slice(0, 2));
  expect(noDeposit[0]).toMatchObject({ x: 0, y: 2, size: 0, missing: true });
  expect(noDeposit[1]).toMatchObject({ x: 100, y: 3, size: 0, missing: true });
});

test("CFFEX IC bubble keeps turnover position and deposit area through a return gap", () => {
  const dates = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
    "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12"];
  const turnover = [122, 118, 130, 131, 129, 135, 140, 138, 142, 147, 151];
  const deposit = [422, 418, 425, 430, 432, 436, 440, 443, 445, 446, 414];
  const frames = resolveBubbleFrames({ key: "CFFEX.IC", name: "中证500股指", horizontalValues: turnover,
    verticalValues: [1.21, ...Array(9).fill(null), -9.02], sizeValues: deposit }, dates);
  expect(frames).toHaveLength(dates.length);
  for (let index = 1; index <= 9; index++) {
    expect(frames[index]).toMatchObject({ x: turnover[index], size: deposit[index], y: 1.21,
      heldYDay: "2026-09-18", missing: true, source: { x: turnover[index], y: null, size: deposit[index] } });
  }
  expect(frames[10]).toMatchObject({ x: 151, size: 414, y: -9.02, missing: false });
});

test("CFFEX bubble carries the actual latest margin publication day without changing its trading day", () => {
  const frames = resolveBubbleFrames({ key: "CFFEX.IC", name: "中证500股指",
    horizontalValues: [100, 110], verticalValues: [2, 3], sizeValues: [500, 520],
    marginPublicationDayValues: ["2026-09-14", "2026-09-21"] },
  ["2026-09-18", "2026-10-16"]);
  expect(frames[0]).toMatchObject({ size: 500, missing: false,
    source: { marginPublicationDay: "2026-09-14" } });
  expect(frames[1]).toMatchObject({ size: 520, missing: false,
    source: { marginPublicationDay: "2026-09-21" } });
});

test("position spread changes fill exactly at line crossings and breaks on missing days", () => {
  const regions = buildSpreadRegions([20, 10, null, 10, 20], [10, 20, null, 20, 10]);
  expect(regions.map(item => item.side)).toEqual(["long", "short", "short", "long"]);
  expect(regions[0].points[1]).toEqual([.5, 15]);
  expect(regions[1].points[0]).toEqual([.5, 15]);
  expect(regions.every(item => item.points.every(([x]) => x < 2 || x > 2))).toBe(true);
});

test("all published seats preserve missing days and genuine zero totals", () => {
  expect(sumPublishedValues([
    { key: "a", name: "甲", values: [null, 0, 2] },
    { key: "b", name: "乙", values: [null, null, 3] },
  ], ["day1", "day2", "day3"])).toEqual([null, 0, 5]);
});
