import { expect, test } from "@playwright/test";
import { resolveBubbleFrames } from "../src/domain/futuresBubbleMotion";

test("missing direction is carried only for position while source and counts remain missing", () => {
  const dates = ["2026-10-23", "2026-10-26", "2026-10-27"];
  const frames = resolveBubbleFrames({
    key: "DCE.M", name: "豆粕", horizontalValues: [100, 0, 0],
    verticalValues: [2.4, null, -1], sizeValues: [5, 0, 0],
    dailyReturnValues: [1.2, null, -3.4], tenDayReturnValues: [2.0, null, -1.1],
    directionUpCountValues: [6, null, 6], directionDownCountValues: [3, null, 4],
    directionFlatCountValues: [1, null, 1], directionWindowCountValues: [10, null, 11],
    directionStatusValues: ["SHORT", "MISSING", "SHORT"],
    directionReasonValues: [null, "窗口含缺报日", null],
  }, dates);
  expect(frames[0]).toMatchObject({ y: 2.4, missing: false,
    source: { y: 2.4, dailyReturn: 1.2, tenDayReturn: 2,
      directionUpCount: 6, directionDownCount: 3, directionFlatCount: 1, directionWindowCount: 10 } });
  expect(frames[1]).toMatchObject({ x: 0, y: 2.4, size: 0, heldYDay: dates[0], missing: true,
    source: { x: 0, y: null, size: 0, dailyReturn: null, tenDayReturn: null,
      directionUpCount: null, directionWindowCount: null, directionStatus: "MISSING",
      directionReason: "窗口含缺报日" } });
  expect(frames[2]).toMatchObject({ y: -1, missing: false, source: { y: -1, dailyReturn: -3.4 } });
});

test("a never observed direction remains missing rather than becoming a real zero", () => {
  const [frame] = resolveBubbleFrames({ key: "CZCE.JR", name: "粳稻", horizontalValues: [0],
    verticalValues: [null], sizeValues: [0], directionStatusValues: ["CALENDAR_UNVERIFIED"],
    directionReasonValues: ["交易日历待核"] }, ["2026-10-27"]);
  expect(frame).toMatchObject({ x: 0, y: 0, size: 0, missing: true,
    source: { x: 0, y: null, size: 0, directionStatus: "CALENDAR_UNVERIFIED",
      directionReason: "交易日历待核" } });
  expect(frame?.heldYDay).toBeUndefined();
});
