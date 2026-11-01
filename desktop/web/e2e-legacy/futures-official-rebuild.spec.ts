import { expect, test } from "@playwright/test";
import { bubbleColorsFor, minimumBubbleColorDistance, minimumBubbleTextContrast } from "../src/domain/futuresBubbleColors";
import { fluidBubbleValue, fluidBubbleVisibility } from "../src/domain/futuresBubbleMotion";
import { buildSpreadRegions } from "../src/domain/futuresSpreadArea";
import { sumPublishedValues } from "../src/domain/futuresPublishedTotals";

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

const dates = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"];
const score = { tradeDate: dates.at(-1), weekEndDate: dates.at(-1), totalScore: 36.5,
  breadthScore: 50, streakScore: 45, magnitudeScore: 25, turnoverScore: 10, dataQualityStatus: "PASS",
  coverage: { direction: 1, turnover: 1, missingExchanges: [] } };
const dailyHeat = dates.map((tradeDate, index) => ({ ...score, tradeDate,
  totalScore: 30 + index, breadthScore: 44 + index, streakScore: index < 4 ? 0 : 45,
  upCount: 100 + index, downCount: 90 - index, flatCount: 3,
  streakUpCount: 40 + index, streakDownCount: 30 - index,
  upWeightedReturnPct: .3 + index * .1, downWeightedReturnPct: -.2 - index * .05,
  upTurnoverCny: 12_000_000_000 + index * 1_000_000_000,
  downTurnoverCny: 9_000_000_000 - index * 500_000_000 }));
const metrics = Array.from({ length: 45 }, (_, index) => ({ key: `member-${index}`, name: `期货公司${index}`,
  values: dates.map((_, day) => index === 5 && day === 2 ? null : (45 - index) * 10 + day) }));
const products = Array.from({ length: 45 }, (_, index) => ({ key: `DCE.P${index}`, name: `品种${index}`,
  values: dates.map((_, day) => (45 - index) * 100 + day),
  xValues: dates.map((_, day) => index === 5 && (day === 2 || day === 3) ? null : day === 0 ? 0 : -1),
  yValues: dates.map((_, day) => index === 5 && day === 2 ? null : index - day),
  sizeValues: dates.map((_, day) => index === 5 && day === 2 ? null : index * 10 + 1 + day * 20),
  weeklyStreakValues: dates.map(() => index % 5) }));

test("daily heat, all bubble products and signed ranking playback", async ({ page }) => {
  const queries: string[] = [];
  const pageErrors: string[] = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.route("**/api/futures/**", async route => {
    const url = new URL(route.request().url()); queries.push(url.pathname + url.search);
    if (url.pathname.endsWith("weekly-heat")) {
      await route.fulfill({ json: { available: true, points: dailyHeat,
        latest: dailyHeat.at(-1), formulaVersion: "daily-v7", limitations: [], config: {
          weights: { breadth: .35, streak: .25, magnitude: .25, turnover: .15 }, score: { min: -100, max: 100 },
          stateBands: [{ min: -100, max: 0, label: "偏空" }, { min: 0, max: 100, label: "偏多" }] } } }); return;
    }
    if (url.pathname.endsWith("market-distributions")) {
      const returnValues = (index: number) => dates.map((_, day) => index === 5 && day === 2 ? null : (index % 9 - 4) * .15 + day * .02);
      const marketSeries = url.searchParams.get("metric") === "return" ? products.map((item, index) => ({
        ...item, values: returnValues(index), yValues: returnValues(index),
        xValues: dates.map((_, day) => index === 5 && (day === 2 || day === 3) ? null : day === 0 ? 0 : .02),
      })) : products;
      await route.fulfill({ json: { available: true, metric: url.searchParams.get("metric"), unit: "张", dates,
        totals: dates.map((_, i) => i * 100), series: marketSeries, baselineDay: dates.at(-1),
        coverage: dates.map(tradeDate => ({ tradeDate, dataQualityStatus: "PASS", missingExchanges: [] })),
        formulaVersion: "market-v4", limitations: [], windowTradingDays: Number(url.searchParams.get("window") || 20) } }); return;
    }
    if (url.pathname.endsWith("ranking-directory")) {
      await route.fulfill({ json: { available: true, latestDay: dates.at(-1), baselineDay: dates.at(-1),
        members: [{ key: "member-0", name: "期货公司0", position: 450 }],
        products: [{ key: "DCE.P0", name: "品种0", contracts: ["P02611"], position: 450 }], dataVersion: "fixture" } }); return;
    }
    if (url.pathname.endsWith("ranking-series") && url.searchParams.get("view") === "product-members") {
      const latestValues = (overrides: Record<number, number | null>) => metrics.map((row, position) =>
        position in overrides ? { ...row, values: row.values.map((value, day) => day === dates.length - 1 ? overrides[position] : value) } : row);
      const setHistory = (rows: typeof metrics, values: Array<number | null>) => rows.map((row, position) =>
        position === 0 ? { ...row, values } : row);
      const longPosition = setHistory(latestValues({ 0: null, 2: 0 }), [20, 40, 25, 45, null]);
      const shortPosition = setHistory(latestValues({ 0: null, 1: null, 2: 0 }), [30, 20, 35, 25, null]);
      const netLongPosition = setHistory(latestValues({ 0: null, 1: null, 2: 0, 3: 0 }), [0, 20, 0, 20, null]);
      const netShortPosition = setHistory(latestValues({ 0: null, 1: null, 2: 0, 3: 0 }), [10, 0, 10, 0, null]);
      await route.fulfill({ json: { available: true, dates, baselineDay: dates.at(-1), latestDay: dates.at(-1),
        metrics: { gross: metrics, longPosition, shortPosition,
          netLongPosition, netShortPosition }, members: [], products: [],
        coverage: dates.map(tradeDate => ({ tradeDate, missingExchanges: [] })), dataVersion: "fixture" } }); return;
    }
    if (url.pathname.endsWith("ranking-series") && url.searchParams.get("view") === "member-products") {
      const withZero = metrics.map((row, position) => position === 2
        ? { ...row, values: row.values.map((value, day) => day === dates.length - 1 ? 0 : value) } : row);
      const withFirstHistory = (rows: typeof metrics, values: number[]) => rows.map((row, position) =>
        position === 0 ? { ...row, values } : row);
      await route.fulfill({ json: { available: true, dates, baselineDay: dates.at(-1), latestDay: dates.at(-1),
        metrics: { gross: metrics,
          longPosition: withFirstHistory(withZero, [20, 40, 25, 45, 30]),
          shortPosition: withFirstHistory(withZero, [30, 20, 35, 25, 40]) }, members: [], products: [],
        coverage: dates.map(tradeDate => ({ tradeDate, missingExchanges: [] })), dataVersion: "fixture" } }); return;
    }
    await route.fulfill({ json: { available: true, dates, baselineDay: dates.at(-1), latestDay: dates.at(-1),
      metrics: { gross: metrics, longPosition: metrics, shortPosition: metrics,
        netLongPosition: metrics, netShortPosition: metrics }, members: [], products: [],
      coverage: dates.map(tradeDate => ({ tradeDate, missingExchanges: [] })), dataVersion: "fixture" } });
  });
  await page.goto("/futures/");
  await expect(page.locator('[data-test="futures-page"]')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-test="futures-weekly-heat"] [data-test="heat-gauge"]')).toHaveCount(1);
  await expect(page.locator('[data-test="futures-weekly-heat"] [data-test="heat-fact"]')).toHaveCount(4);
  await expect(page.locator('[data-test="futures-weekly-heat"] h2')).toHaveText("多空热度");
  await expect(page.locator('[data-test="futures-weekly-heat"] header time')).toHaveCount(0);
  await expect(page.locator('[data-test="futures-weekly-heat"] [data-test="futures-timeline"]')).toHaveCount(1);
  await expect(page.locator('[data-test="futures-weekly-heat"]')).toContainText("上涨");
  await expect(page.locator('[data-test="futures-weekly-heat"]')).not.toContainText("连续周热度 25%");
  await expect(page.locator('[data-test="futures-bubble"]')).toHaveCount(4);
  for (const bubble of await page.locator('[data-test="futures-bubble"]').all())
    await expect(bubble).toHaveAttribute("data-point-count", "45");
  const bubbles = page.locator('[data-test="futures-bubble"]');
  await expect(bubbles.first()).toHaveAttribute("data-axis-x", "持仓量变化（%）");
  for (const bubble of await bubbles.all())
    await expect(bubble).toHaveAttribute("data-axis-y", "横轴逐日变化（百分点／交易日）");
  await expect(bubbles.last()).toHaveAttribute("data-axis-x", "当日加权涨跌幅（%）");
  const verticalTitle = (await bubbles.last().getAttribute("data-axis-y-title"))!;
  expect(verticalTitle).toContain("横\n轴\n逐\n日\n变\n化");
  expect(verticalTitle).toContain("百分点\n／\n交易日");
  const productColors = JSON.parse((await bubbles.first().getAttribute("data-colors"))!);
  expect(new Set(Object.values(productColors)).size).toBe(16);
  expect(Number(await bubbles.first().getAttribute("data-min-color-distance"))).toBeGreaterThan(10);
  expect(Number(await bubbles.first().getAttribute("data-min-text-contrast"))).toBeGreaterThan(6);
  await expect(bubbles.first()).toHaveAttribute("data-label-count", "0");
  expect(JSON.parse((await bubbles.last().getAttribute("data-colors"))!)).toEqual(productColors);
  const cardBoxes = await page.locator('.bubble-grid > .distribution-card').evaluateAll(nodes => nodes.map(node => {
    const box = node.getBoundingClientRect(); return { x: box.x, y: box.y };
  }));
  expect(Math.abs(cardBoxes[0].y - cardBoxes[1].y)).toBeLessThan(2);
  expect(cardBoxes[1].x).toBeGreaterThan(cardBoxes[0].x);
  expect(cardBoxes[2].y).toBeGreaterThan(cardBoxes[0].y);
  await expect(page.locator('[data-test="futures-market-distributions"] input[type="range"]')).toHaveCount(0);
  await bubbles.first().scrollIntoViewIfNeeded();
  const bubbleBox = (await bubbles.first().boundingBox())!;
  const initialView = JSON.parse((await bubbles.first().getAttribute("data-view"))!);
  await page.mouse.move(bubbleBox.x + 35, bubbleBox.y + 150);
  await page.mouse.down();
  await page.mouse.move(bubbleBox.x + 95, bubbleBox.y + 190, { steps: 5 });
  await page.mouse.up();
  const draggedView = JSON.parse((await bubbles.first().getAttribute("data-view"))!);
  expect(draggedView.xmin).toBeLessThan(initialView.xmin);
  expect(draggedView.ymin).toBeGreaterThan(initialView.ymin);
  await page.locator('[data-test="futures-open_interest-distribution"]').getByRole("button", { name: "复位气泡视图" }).click();
  expect(JSON.parse((await bubbles.first().getAttribute("data-view"))!)).toEqual(initialView);
  await expect(page.locator('[data-test="futures-signed-bar"]')).toHaveCount(3);
  await expect(page.locator('[data-test="futures-position-spread-area"]')).toHaveCount(3);
  for (const area of await page.locator('[data-test="futures-position-spread-area"]').all()) {
    expect(Number(await area.getAttribute("data-red-regions"))).toBeGreaterThan(0);
    expect(Number(await area.getAttribute("data-green-regions"))).toBeGreaterThan(0);
  }
  for (const signedBar of await page.locator('[data-test="futures-signed-bar"]').all()) {
    await expect(signedBar).toHaveAttribute("data-visible-count", "10");
    await expect(signedBar).toHaveAttribute("data-orientation", "horizontal");
    await expect(signedBar).toHaveAttribute("data-scroll-direction", "vertical");
  }
  const barPanels = await page.locator('[data-test="futures-product-member-distribution"] .signed-bar-grid > article').evaluateAll(nodes => nodes.map(node => {
    const box = node.getBoundingClientRect(); return { x: box.x, y: box.y };
  }));
  expect(Math.abs(barPanels[0].y - barPanels[1].y)).toBeLessThan(2);
  expect(barPanels[1].x).toBeGreaterThan(barPanels[0].x);
  const rankScroll = page.locator('[data-test="futures-product-member-distribution"] [data-test="futures-signed-scroll"]').first();
  expect(await rankScroll.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
  const companyBar = page.locator('[data-test="futures-member-distribution"] [data-test="futures-signed-bar"]');
  await expect(companyBar).toHaveAttribute("data-row-count", "44");
  expect((await companyBar.getAttribute("data-row-keys"))!.split(",")).not.toContain("member-2");
  const productBars = page.locator('[data-test="futures-product-member-distribution"] [data-test="futures-signed-bar"]');
  const productSection = page.locator('[data-test="futures-product-member-distribution"]');
  const totals = productSection.locator('[data-test="product-position-totals"]');
  await expect(totals.locator('article[data-metric="longPosition"] strong')).toHaveText("9,642");
  await expect(totals.locator('article[data-metric="shortPosition"] strong')).toHaveText("9,198");
  await expect(totals.locator('article[data-metric="netLongPosition"] strong')).toHaveText("8,774");
  await expect(totals.locator('article[data-metric="netShortPosition"] strong')).toHaveText("8,774");
  await expect(productSection.locator('[data-test="futures-position-spread-area"]')).toHaveCount(2);
  await expect(productSection.locator('.spread-chart h3').first()).toHaveText("全部已公布席位多空持仓历史");
  await expect(productBars.first()).toHaveAttribute("data-selection-effect", "none");
  await expect(productBars.nth(0)).toHaveAttribute("data-row-count", "43");
  await expect(productBars.nth(1)).toHaveAttribute("data-row-count", "41");
  const grossKeys = (await productBars.nth(0).getAttribute("data-row-keys"))!.split(",");
  expect(grossKeys).not.toContain("member-0");
  expect(grossKeys).not.toContain("member-2");
  expect(grossKeys).toContain("member-1");
  const netKeys = (await productBars.nth(1).getAttribute("data-row-keys"))!.split(",");
  for (const hidden of ["member-0", "member-1", "member-2", "member-3"]) expect(netKeys).not.toContain(hidden);
  await productSection.locator('[aria-label="期货席位"]').click();
  await page.getByRole("option", { name: "期货公司4", exact: true }).click();
  await expect(productBars.first()).toHaveAttribute("data-selected-key", "member-4");
  await expect(productBars.first()).toHaveAttribute("data-selection-effect", "glow-and-label");
  await expect(productSection.locator('.spread-chart h3').first()).toHaveText("所选席位多空持仓历史");
  await expect(totals.locator('article[data-metric="longPosition"] strong')).toHaveText("9,642");
  await productSection.locator('[aria-label="期货席位"]').click();
  await page.getByRole("option", { name: "全部席位", exact: true }).click();
  await expect(productBars.first()).toHaveAttribute("data-selected-key", "");
  await expect(productSection.locator('.spread-chart h3').first()).toHaveText("全部已公布席位多空持仓历史");
  await expect(page.locator('[data-test="futures-market-overview"]')).toHaveCount(1);
  await expect(page.locator('[data-test="futures-market-overview"] [data-test="futures-timeline"]')).toHaveAttribute("data-series-count", "4");
  await expect(page.locator('[data-test="futures-market-distributions"] [data-test="futures-timeline"][data-area="true"]')).toHaveCount(0);
  await expect(page.locator(".weight-control")).toHaveCount(0);
  await expect(page.locator(".pie-chart")).toHaveCount(0);
  await expect(page.locator('[data-test="line-pagination"]')).toHaveCount(0);
  const company = page.locator('[data-test="futures-member-distribution"]');
  const bubbleKeys = await page.locator('[data-test="futures-bubble"]').first().getAttribute("data-scale");
  const networkBefore = queries.length;
  const animationClock = new Date(Date.now() + 60_000);
  await page.clock.install({ time: animationClock });
  await page.clock.pauseAt(new Date(animationClock.getTime() + 1000));
  const playback = company.locator('[data-test="ranking-playback"]');
  await expect(playback.getByLabel("席位播放倍速")).toHaveValue("2");
  await playback.getByLabel("席位播放倍速").selectOption("1");
  await playback.getByRole("button", { name: "播放" }).click();
  await expect(playback.locator("time")).toHaveText(dates[0]);
  await page.clock.runFor(500);
  await expect(company.locator('[data-test="futures-signed-bar"]')).toHaveAttribute("data-row-count", "44");
  await page.clock.runFor(500);
  await expect(playback.locator("time")).toHaveText(dates.at(-1)!);
  await expect(playback.getByRole("button", { name: "播放" })).toHaveAttribute("aria-pressed", "false");
  const bubblePlayback = page.locator('[data-test="bubble-playback"]');
  await expect(bubblePlayback.getByLabel("气泡播放倍速")).toHaveValue("2");
  const trough = bubblePlayback.locator('[data-test="bubble-date-track"]');
  await trough.scrollIntoViewIfNeeded();
  let troughBox = (await trough.boundingBox())!;
  await bubblePlayback.getByRole("button", { name: "播放" }).click();
  await page.clock.runFor(40);
  const positionOne = Number(await bubblePlayback.getAttribute("data-position"));
  expect(positionOne).toBeGreaterThan(0);
  expect(positionOne).toBeLessThan(1);
  const frameOne = await bubbles.first().screenshot();
  await page.mouse.move(troughBox.x + troughBox.width * .75, troughBox.y + troughBox.height / 2);
  await expect(bubblePlayback.getByRole("button", { name: "暂停" })).toHaveAttribute("aria-pressed", "true");
  await page.clock.runFor(40);
  const positionTwo = Number(await bubblePlayback.getAttribute("data-position"));
  expect(positionTwo).toBeGreaterThan(positionOne);
  expect(positionTwo).toBeLessThan(1);
  const frameTwo = await bubbles.first().screenshot();
  expect(frameOne.equals(frameTwo)).toBe(false);
  await page.clock.runFor(180);
  await expect(bubbles.first()).toHaveAttribute("data-point-count", "44");
  await page.clock.runFor(260);
  await expect(bubblePlayback.locator("time")).toHaveText(dates.at(-1)!);
  await expect(bubblePlayback.getByRole("button", { name: "播放" })).toHaveAttribute("aria-pressed", "false");
  await bubblePlayback.getByLabel("气泡播放倍速").selectOption("4");
  await bubblePlayback.getByRole("button", { name: "播放" }).click();
  await page.clock.runFor(80);
  expect(Number(await bubblePlayback.getAttribute("data-position"))).toBeGreaterThan(1);
  await bubblePlayback.getByRole("button", { name: "暂停" }).click();
  await trough.scrollIntoViewIfNeeded();
  troughBox = (await trough.boundingBox())!;
  await page.mouse.move(troughBox.x + troughBox.width / 4, troughBox.y + troughBox.height / 2);
  await page.clock.runFor(160);
  await expect(bubblePlayback.locator("time")).toHaveText(dates[1]);
  await page.mouse.move(troughBox.x + troughBox.width * .75, troughBox.y + troughBox.height / 2);
  await expect(bubblePlayback.locator("time")).toHaveText(dates[3]);
  await page.mouse.move(troughBox.x + 1, troughBox.y + troughBox.height / 2);
  await expect(bubblePlayback.locator("time")).toHaveText(dates[0]);
  await page.mouse.down();
  await page.mouse.move(troughBox.x + troughBox.width - 1, troughBox.y + troughBox.height / 2, { steps: 8 });
  await page.mouse.up();
  await expect(bubblePlayback.locator("time")).toHaveText(dates[4]);
  expect(Number(await bubblePlayback.getAttribute("data-position"))).toBeGreaterThan(3.9);
  const productPlayback = page.locator('[data-test="futures-product-member-distribution"] [data-test="ranking-playback"]');
  await productPlayback.getByRole("button", { name: "播放" }).click();
  await page.clock.runFor(1000);
  await expect(productPlayback.locator("time")).toHaveText(dates.at(-1)!);
  expect(queries.length).toBe(networkBefore);
  await expect(page.locator('[data-test="futures-bubble"]').first()).toHaveAttribute("data-scale", bubbleKeys!);
  await expect(page.locator('[data-test="futures-qa"] details[open]')).toHaveCount(0);
  expect(pageErrors).toEqual([]);
});
