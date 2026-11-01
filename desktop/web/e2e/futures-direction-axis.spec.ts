import { expect, test } from "@playwright/test";
import { directionAmount, directionPosition } from "../src/domain/futuresDirectionAxis";

test("signed direction scale preserves zero, ordering, extremes and exact values", () => {
  const values = [-1000, -100, -10, -1, 0, 1, 10, 100, 1000];
  const positions = values.map(directionPosition);
  expect(directionPosition(0)).toBe(0);
  for (let index = 0; index < values.length; index++) {
    expect(directionAmount(positions[index])).toBeCloseTo(values[index], 9);
    expect(positions[index]).toBeCloseTo(-directionPosition(-values[index]), 12);
    if (index) expect(positions[index]).toBeGreaterThan(positions[index - 1]);
  }
  // Low-amplitude trend starts get more canvas distance per real point.
  expect(directionPosition(2) - directionPosition(1))
    .toBeGreaterThan(directionPosition(102) - directionPosition(101));
});
