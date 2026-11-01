import { expect, test, type Page } from "@playwright/test";
import { turnoverPosition, type TurnoverAxis } from "../src/domain/futuresTurnoverAxis";

const dates = ["2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"];
const products = [{ key: "DCE.M", name: "豆粕（M）" }, { key: "SHFE.AG", name: "白银（AG）" }];
const cffexProducts = ["IM", "IC", "IF", "IH", "TL", "T", "TF", "TS"]
  .map(code => ({ key: `CFFEX.${code}`, name: `中金所品种（${code}）` }));
const pageErrors = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const errors: string[] = []; pageErrors.set(page, errors);
  page.on("pageerror", error => errors.push(error.message));
});
test.afterEach(({ page }) => { expect(pageErrors.get(page)).toEqual([]); });
async function openFutures(page: Page, bubbleGap = false, zeroActivity = false,
  delayedDirectory = false, sourceGap = false, marginGap = false, dense = false,
  separateDirection = false, highlight = false, fixtureDates: string[] = dates): Promise<string[]> {
  const dates = fixtureDates;
  const requests: string[] = [];
  let completedRevision = "fixture-initial";
  await page.route("**/api/futures/**", async route => {
    const url = new URL(route.request().url()); requests.push(url.pathname + url.search);
    if (url.pathname.endsWith("update-status")) {
      await route.fulfill({ json: { autoEnabled: true, checkIntervalSeconds: 900,
        latestObserved: { tdxLatestDay: "2026-09-29", officialTurnoverLatestDay: "2026-09-30",
          officialMarginLatestDay: "2026-09-30" }, jobs: [], completedRevision,
        localQuality: { queryIndexStatus: "CURRENT", contractCount: 2, tdxValidCount: 2, capitalCount: 2 },
        checkedAt: "2026-09-30T12:00:00Z" } }); return;
    }
    if (url.pathname.endsWith("check-updates")) {
      completedRevision = "fixture-after-check";
      await route.fulfill({ status: 202, json: { items: [] } }); return;
    }
    if (url.pathname.endsWith("contract-info")) {
      await route.fulfill({ json: { products: [], coverage: {}, limitations: [] } }); return;
    }
    if (url.pathname.endsWith("weekly-heat")) {
      await route.fulfill({ json: { available: false, points: [], limitations: [], formulaVersion: "fixture" } }); return;
    }
    if (url.pathname.endsWith("ranking-directory")) {
      if (delayedDirectory) await new Promise(resolve => setTimeout(resolve, 8_000));
      await route.fulfill({ json: { available: true, latestDay: dates.at(-1), baselineDay: dates.at(-1),
        members: [{ key: "member-a", name: "合成期货公司甲", position: 200 }],
        products: products.map(item => ({ ...item, contracts: ["M2611"] })), dataVersion: "fixture" } }); return;
    }
    if (url.pathname.endsWith("ranking-series")) {
      const entities = url.searchParams.get("view") === "member-products" ? products
        : [{ key: "member-a", name: "合成期货公司甲" }, { key: "member-b", name: "合成期货公司乙" }];
      const rows = (first: Array<number | null>, second: Array<number | null>) => entities.map((item, index) =>
        ({ ...item, values: index ? second : first }));
      await route.fulfill({ json: { available: true, dates, latestDay: dates.at(-1), baselineDay: dates.at(-1),
        metrics: { gross: rows([200, null, 240, 280], [100, null, 120, 140]),
          longPosition: rows([120, null, 140, 160], [60, null, 70, 80]),
          shortPosition: rows([80, null, 100, 120], [40, null, 50, 60]),
          netLongPosition: rows([40, null, 40, 40], [0, null, 0, 0]),
          netShortPosition: rows([0, null, 0, 0], [20, null, 20, 20]) },
        members: [], products: [], coverage: dates.map(tradeDate => ({ tradeDate, missingExchanges: [] })),
        dataVersion: "fixture" } }); return;
    }
    if (url.pathname.endsWith("market-distributions")) {
      if (delayedDirectory && url.searchParams.has("snapshot_only")) {
        await route.fulfill({ json: { available: false, metric: url.searchParams.get("metric"), dates: [] } }); return;
      }
      const metric = url.searchParams.get("metric");
      const distributionProducts = marginGap ? [...products, ...cffexProducts] : dense
        ? [...products, ...[0, 10, 20, 50, 200, 1000, 4000].map((_, index) => ({
          key: `FIX.${index}`, name: `测试品种${index}`,
        }))] : products;
      const directionMissing = (index: number, dayIndex: number) =>
        (sourceGap && !index && dayIndex === 3) || (zeroActivity && index === 1 && dayIndex === 3)
          || (bubbleGap && !index && dayIndex === 1);
      await route.fulfill({ json: { available: true, metric, dates, baselineDay: dates.at(-1),
        totals: dates.map(() => 2e10), series: distributionProducts.map((item, index) => ({ ...item,
          values: dates.map((_, dayIndex) => {
            if (marginGap && item.key.startsWith("CFFEX.") && dayIndex === 3 && metric === "deposit") return null;
            if (sourceGap && !index && dayIndex === 3) return null;
            if (dense && index >= 2 && metric === "turnover") return [0, 10, 20, 50, 200, 1000, 4000][index - 2] * 1e8;
            if (zeroActivity && index === 1 && dayIndex === 3 && metric !== "return") return 0;
            if (bubbleGap && !index && metric === "turnover" && dayIndex === 2) return null;
            if (metric === "return") return index ? -1 : 1.5;
            if (index) return 3e9;
            if (bubbleGap && metric === "turnover") return [1e10, 1.2e10, 1.4e10, 1.6e10][dayIndex];
            if (bubbleGap && metric === "deposit") return [6e9, 8e9, 1e10, 1.2e10][dayIndex];
            return 1e10;
          }),
          xValues: dates.map(() => 0), yValues: dates.map(() => 0), sizeValues: dates.map(() => 1e9),
          tenDayReturnValues: dates.map((_, dayIndex) => sourceGap && !index && dayIndex === 3 ? null
            : zeroActivity && index === 1 && dayIndex === 3 ? null
            : bubbleGap && !index && dayIndex === 1 ? null : index ? -1 : 1.5),
          directionSumValues: dates.map((_, dayIndex) => directionMissing(index, dayIndex) ? null
            : separateDirection && !index && dayIndex === 3 ? 4.75 : index ? -1 : 1.5),
          directionUpCountValues: dates.map((_, dayIndex) => directionMissing(index, dayIndex) ? null : 3),
          directionDownCountValues: dates.map((_, dayIndex) => directionMissing(index, dayIndex) ? null : 1),
          directionFlatCountValues: dates.map((_, dayIndex) => directionMissing(index, dayIndex) ? null : 0),
          directionWindowCountValues: dates.map((_, dayIndex) => directionMissing(index, dayIndex) ? null : 4),
          directionStatusValues: dates.map((_, dayIndex) => directionMissing(index, dayIndex) ? "MISSING" : "SHORT"),
          directionReasonValues: dates.map((_, dayIndex) => directionMissing(index, dayIndex) ? "窗口中存在缺报或无有效持仓" : null),
          shortMove3Values: dates.map((_, dayIndex) => highlight && !index ? [1, 2, 3, 4][dayIndex] : null),
          shortRankValues: dates.map((_, dayIndex) => highlight && !index ? [.89, .91, null, .95][dayIndex] : null),
          shortSampleCountValues: dates.map(() => highlight ? 85 : null),
          shortCompareStatusValues: dates.map((_, dayIndex) => highlight && dayIndex !== 2 ? "COMPLETE" : "INCOMPLETE"),
          tenDayCoverageValues: dates.map(() => 1),
          depositReasonValues: dates.map((_, dayIndex) => marginGap && item.key.startsWith("CFFEX.") && dayIndex === 3
            ? "3个有持仓月份合约缺同日有效投机保证金率" : null),
          missingMarginContractCountValues: dates.map((_, dayIndex) => marginGap && item.key.startsWith("CFFEX.") && dayIndex === 3 ? 3 : 0) })),
        coverage: dates.map((tradeDate, index) => ({ tradeDate,
          dataQualityStatus: sourceGap && index === 3 ? "PARTIAL" : "PASS",
          missingExchanges: sourceGap && index === 3 ? ["DCE"] : [] })),
        formulaVersion: "fixture", limitations: [] } }); return;
    }
    await route.fulfill({ json: {} });
  });
  await page.goto("/futures/");
  if (delayedDirectory) return requests;
  await expect(page.locator('[data-test="futures-signed-bar"]')).toHaveCount(5);
  await expect(page.locator('.timeline-actions time')).toHaveText(dates.at(-1)!);
  return requests;
}

test("bubble loads while the independent ranking directory is slow", async ({ page }) => {
  await openFutures(page, false, false, true);
  await expect(page.locator('[data-test="futures-bubble"]')).toHaveCount(1, { timeout: 5_000 });
  await expect(page.locator('[data-test="futures-bubble"]')).toHaveAttribute("data-point-count", "2");
});

test("page reads auto-update status and reloads after a completed source check", async ({ page }) => {
  const requests = await openFutures(page);
  await expect(page.locator('[data-test="futures-update-summary"]')).toContainText("服务按交易日自动检查更新");
  const initialLoads = requests.filter(path => path.includes("weekly-heat")).length;
  await page.getByRole("button", { name: "立即检查更新" }).click();
  await expect.poll(() => requests.filter(path => path.includes("weekly-heat")).length)
    .toBeGreaterThan(initialLoads);
  expect(requests.some(path => path.includes("check-updates"))).toBe(true);
});

test("returning to the page checks status immediately and ignores late older responses", async ({ page }) => {
  await openFutures(page);
  const summary = page.locator('[data-test="futures-update-summary"]');
  await expect(summary).toContainText("服务按交易日自动检查更新");
  let release!: () => void, entered!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const started = new Promise<void>(resolve => { entered = resolve; });
  let calls = 0;
  await page.route('**/api/futures/update-status', async route => {
    const first = ++calls === 1;
    if (first) { entered(); await gate; }
    await route.fulfill({json: {autoEnabled: !first, checkIntervalSeconds: 900,
      latestObserved: {tdxLatestDay: '2026-09-29', officialTurnoverLatestDay: '2026-09-30', officialMarginLatestDay: '2026-09-30'},
      jobs: [], completedRevision: 'fixture-initial', localQuality: {queryIndexStatus: 'CURRENT', contractCount: 2, tdxValidCount: 2, capitalCount: 2}, checkedAt: '2026-09-30T12:00:00Z'}});
  });
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await started;
  const response = page.waitForResponse(value => value.url().includes('/api/futures/update-status'));
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await response;
  const late = page.waitForResponse(value => value.url().includes('/api/futures/update-status'));
  release();
  await late;
  await expect.poll(() => calls).toBe(2);
  await expect(summary).toContainText('服务按交易日自动检查更新');
});

test("missing official exchange day labels carried bubbles as display placeholders", async ({ page }) => {
  await openFutures(page, false, false, false, true);
  const notice = page.locator('[data-test="futures-bubble-source-gap"]');
  await expect(notice).toContainText("2026-09-30 大商所 1 个品种");
  await expect(notice).toContainText("沿用2026-09-29的显示位置和大小");
  await expect(page.locator('[data-test="futures-bubble"]')).toHaveAttribute("data-point-count", "2");
  await page.locator('[data-test="futures-trading-date-picker"]').first().locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-29"]').click();
  await expect(notice).toHaveCount(0);
});

test("missing CFFEX margins explain hollow bubbles without hiding available metrics", async ({ page }) => {
  await openFutures(page, false, false, false, false, true);
  const notice = page.locator('[data-test="futures-bubble-margin-gap"]');
  await expect(notice).toContainText("2026-09-30 中金所 8 个品种的沉淀资金缺报");
  await expect(notice).toContainText("品种及已核验的成交额、方向累计仍显示");
  await expect(page.locator('[data-test="futures-bubble-source-gap"]')).toHaveCount(0);
  await expect(page.locator('[data-test="futures-bubble"]')).toHaveAttribute("data-incomplete-count", "8");
  await page.locator('[data-test="futures-trading-date-picker"]').first().locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-29"]').click();
  await expect(notice).toHaveCount(0);
  await expect(page.locator('[data-test="futures-bubble"]')).toHaveAttribute("data-incomplete-count", "0");
});

test("default bars render both directions and all seats; single-direction nets stay visible", async ({ page }) => {
  const requests = await openFutures(page);
  expect(requests.some(url => url.includes("member_key=__all__"))).toBe(true);
  const company = page.locator('[data-test="futures-member-distribution"]');
  await expect(company.locator('.el-select').first()).toContainText("全部席位");
  await expect(company.locator('.signed-bar-panel h3')).toHaveText(["多头和空头", "净多", "净空"]);
  await expect(page.getByText("该日没有可显示持仓", { exact: true })).toHaveCount(0);
  for (const bar of await page.locator('[data-test="futures-signed-bar"]').all())
    await expect(bar.locator("canvas")).toBeVisible();
  const painted = await company.locator('[data-test="futures-signed-bar"]').first().locator("canvas").evaluate(canvas => {
    const node = canvas as HTMLCanvasElement, pixels = node.getContext("2d")!.getImageData(0, 0, node.width, node.height).data;
    let red = 0, green = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i + 3] < 150) continue;
      if (pixels[i] > pixels[i + 1] * 1.3 && pixels[i] > pixels[i + 2] * 1.2) red++;
      if (pixels[i + 1] > pixels[i] * 1.3 && pixels[i + 1] > pixels[i + 2] * 1.1) green++;
    }
    return { red, green };
  });
  expect(painted.red).toBeGreaterThan(100); expect(painted.green).toBeGreaterThan(100);
});

test("current heat shows only three weighted facts and no turnover module", async ({ page }) => {
  await openFutures(page);
  const points = dates.map((tradeDate, index) => ({
    tradeDate, weekEndDate: dates.at(-1), totalScore: 25 + index * .2,
    breadthScore: 35 + index, streakScore: 25, magnitudeScore: 15,
    upCount: 100 + index, downCount: 90 - index, flatCount: 2,
    streakUpCount: 40, streakDownCount: 30,
    upWeightedReturnPct: .5, downWeightedReturnPct: -.3,
    upTurnoverCny: 12_345_000_000, downTurnoverCny: 8_765_000_000,
    dataQualityStatus: "PASS", coverage: { direction: 1, turnover: 1, missingExchanges: [] },
  }));
  await page.route("**/api/futures/weekly-heat**", route => route.fulfill({ json: {
    available: true, points, latest: points.at(-1), formulaVersion: "daily-v8",
    limitations: [], config: { weights: { breadth: .20, streak: .60, magnitude: .20 },
      score: { min: -100, max: 100 }, stateBands: [
        { min: -100, max: 0, label: "偏空" }, { min: 0, max: 100, label: "偏多" },
      ] },
  } }));
  await page.reload();
  const heat = page.locator('[data-test="futures-weekly-heat"]');
  await expect(heat.locator("h2")).toHaveText("多空热度");
  await expect(heat.locator('[data-test="heat-gauge"]')).toHaveCount(1);
  await expect(heat.locator('[data-test="heat-fact"]')).toHaveCount(3);
  await expect(heat.locator('[data-test="futures-timeline"]')).toHaveAttribute("data-series-count", "4");
  await expect(heat).toContainText("上涨");
  await expect(heat.getByText("成交额", { exact: true })).toHaveCount(0);
  await expect(heat.getByText("成交额多空热度", { exact: true })).toHaveCount(0);
  await expect(page.locator('[data-test="futures-bubble"]')).toHaveCount(1);
  await expect(page.locator('.weight-control')).toHaveCount(0);
});

test("bubble remains present when one product lacks a daily return", async ({ page }) => {
  await openFutures(page, true);
  const bubble = page.locator('[data-test="futures-bubble"]');
  await expect(bubble).toHaveAttribute("data-point-count", "2");
  const calendar = page.locator('[data-test="futures-trading-date-picker"]').first();
  await calendar.locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-28"]').click();
  await expect(page.locator('.timeline-actions time')).toHaveText("2026-09-28");
  await expect(bubble).toHaveAttribute("data-position", "1");
  await expect(bubble).toHaveAttribute("data-point-count", "2");
  await expect(bubble).toHaveAttribute("data-incomplete-count", "1");
  await expect(page.getByText("该日缺少气泡所需的区间数据")).toHaveCount(0);
  const hoverPoint = await bubble.evaluate(element => {
    const scale = JSON.parse(element.getAttribute("data-scale")!);
    const xInset = (scale.xmax - scale.xmin) * .08;
    const yInset = (scale.ymax - scale.ymin) * .1;
    const x = Math.max(scale.xmin + xInset, Math.min(scale.xmax - xInset, 120));
    const y = Math.max(scale.ymin + yInset, Math.min(scale.ymax - yInset, Math.asinh(1.5 / 10)));
    const radius = (12 + 60 * Math.log1p(80 / 20) / Math.log1p(scale.size / 20)) / 2;
    return { x: 76 + (x - scale.xmin) / (scale.xmax - scale.xmin) * (element.clientWidth - 100) + radius - 1,
      y: 35 + (scale.ymax - y) / (scale.ymax - scale.ymin) * (element.clientHeight - 100) };
  });
  await bubble.scrollIntoViewIfNeeded();
  const box = await bubble.boundingBox();
  await page.mouse.move(box!.x + hoverPoint.x, box!.y + hoverPoint.y);
  await expect(page.getByText("当日缺报", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/空心表示三项中有缺报/)).toBeVisible();
  await calendar.locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-29"]').click();
  // Turnover is absent on this day; the product keeps its last X position and current deposit area.
  await expect(bubble).toHaveAttribute("data-point-count", "2");
  await expect(bubble).toHaveAttribute("data-incomplete-count", "1");
});

test("zero turnover and deposit keep a missing direction hollow", async ({ page }) => {
  await openFutures(page, false, true);
  const bubble = page.locator('[data-test="futures-bubble"]');
  await expect(bubble).toHaveAttribute("data-point-count", "2");
  await bubble.scrollIntoViewIfNeeded();
  const center = await bubble.evaluate(element => {
    const scale = JSON.parse(element.getAttribute("data-scale")!);
    const x = 0;
    const y = Math.max(scale.ymin + (scale.ymax - scale.ymin) * .1,
      Math.min(scale.ymax - (scale.ymax - scale.ymin) * .1, Math.asinh(-1 / 10)));
    return { x: 76 + (x - scale.xmin) / (scale.xmax - scale.xmin) * (element.clientWidth - 100),
      y: 35 + (scale.ymax - y) / (scale.ymax - scale.ymin) * (element.clientHeight - 100) };
  });
  const box = await bubble.boundingBox();
  await page.mouse.move(box!.x + center.x + 2, box!.y + center.y);
  await expect(page.getByText(/方向累计缺报：窗口中存在缺报或无有效持仓/)).toBeVisible();
  await expect(page.getByText(/空心表示三项中有缺报/)).toBeVisible();
  await expect(page.getByText(/方向累计有值，按最小实心圆显示/)).toHaveCount(0);
});

test("direction bubble and selected history use the same value without replacing ten-day return", async ({ page }) => {
  await openFutures(page, false, false, false, false, false, false, true);
  const bubble = page.locator('[data-test="futures-bubble"]');
  await expect(bubble).toHaveAttribute("data-axis-y", "近1～60交易日方向累计（百分点）");
  await expect(bubble).toHaveAccessibleName(/横轴为当日成交额，纵轴为近60个交易日的方向累计/);
  const center = await bubble.evaluate(element => {
    const scale = JSON.parse(element.getAttribute("data-scale")!);
    const x = Math.max(scale.xmin + (scale.xmax - scale.xmin) * .08,
      Math.min(scale.xmax - (scale.xmax - scale.xmin) * .08, 100));
    const y = Math.max(scale.ymin + (scale.ymax - scale.ymin) * .1,
      Math.min(scale.ymax - (scale.ymax - scale.ymin) * .1, Math.asinh(4.75 / 10)));
    return { x: 76 + (x - scale.xmin) / (scale.xmax - scale.xmin) * (element.clientWidth - 100),
      y: 35 + (scale.ymax - y) / (scale.ymax - scale.ymin) * (element.clientHeight - 100) };
  });
  await bubble.scrollIntoViewIfNeeded();
  const box = await bubble.boundingBox();
  await page.mouse.move(box!.x + center.x, box!.y + center.y);
  await expect(page.getByText("+4.75百分点", { exact: true })).toBeVisible();
  await expect(page.getByText("+1.50%", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("3／1／0日", { exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "成交额与涨跌分布选择品种", exact: true }).click();
  await page.getByRole("option", { name: "豆粕（M）", exact: true }).click();
  const history = page.locator('[data-test="futures-triple-metric-chart"]');
  await expect(history).toHaveAttribute("data-series-count", "4");
  await expect(history).toHaveAttribute("data-direction-present", "true");
  await expect(history).toHaveAccessibleName(/近1～60交易日净涨跌幅累计/);
  await history.scrollIntoViewIfNeeded();
  const historyBox = await history.boundingBox();
  await page.mouse.move(historyBox!.x + historyBox!.width - 60, historyBox!.y + historyBox!.height * .35);
  await expect(page.getByText(/近1～60交易日净涨跌幅累计：4\.75 百分点/)).toBeVisible();
  await expect(page.getByText(/近1～10交易日涨跌幅：1\.50 %/)).toBeVisible();
});

test("short-term relative strength is highlighted only for verified comparison days", async ({ page }) => {
  await openFutures(page, false, false, false, false, false, false, false, true);
  const bubble = page.locator('[data-test="futures-bubble"]');
  await expect(bubble).toHaveAttribute("data-short-highlight-count", "1");
  const calendar = page.locator('[data-test="futures-trading-date-picker"]').first();
  await calendar.locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-29"]').click();
  await expect(bubble).toHaveAttribute("data-short-highlight-count", "0");
  await calendar.locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-28"]').click();
  await expect(bubble).toHaveAttribute("data-short-highlight-count", "1");
  await calendar.locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-24"]').click();
  await expect(bubble).toHaveAttribute("data-short-highlight-count", "0");
});

test("quantile turnover axis keeps real tooltip and fixed scale across dates", async ({ page }) => {
  await openFutures(page, false, false, false, false, false, true);
  const bubble = page.locator('[data-test="futures-bubble"]');
  const axis: TurnoverAxis = JSON.parse((await bubble.getAttribute("data-x-scale"))!);
  expect(axis.mode).toBe("quantile");
  expect(axis.referenceDay).toBe("2026-09-30");
  expect(axis.sampleCount).toBe(9);
  await expect(page.locator('[data-test="futures-bubble-axis-note"]')).toContainText("刻度显示真实金额");
  await bubble.scrollIntoViewIfNeeded();
  const zeroY = await bubble.evaluate(element => {
    const bounds = JSON.parse(element.getAttribute("data-scale")!);
    const y = Math.asinh(-1 / 10), inset = (bounds.ymax - bounds.ymin) * .1;
    const visualY = Math.max(bounds.ymin + inset, Math.min(bounds.ymax - inset, y));
    return 35 + (bounds.ymax - visualY) / (bounds.ymax - bounds.ymin) * (element.clientHeight - 100);
  });
  const zeroBox = await bubble.boundingBox();
  await page.mouse.move(zeroBox!.x + 78, zeroBox!.y + zeroY);
  await expect(bubble.getByText("测试品种0", { exact: true })).toBeVisible();
  await expect(bubble.getByText("0.00亿元", { exact: true }).first()).toBeVisible();
  const point = await bubble.evaluate((element, coordinate) => {
    const bounds = JSON.parse(element.getAttribute("data-scale")!);
    const x = coordinate, y = Math.asinh(1.5 / 10);
    const yi = (bounds.ymax - bounds.ymin) * .1;
    const visualY = Math.max(bounds.ymin + yi, Math.min(bounds.ymax - yi, y));
    const radius = (12 + 60 * Math.log1p(100 / 20) / Math.log1p(bounds.size / 20)) / 2;
    return { x: 76 + (x - bounds.xmin) / (bounds.xmax - bounds.xmin) * (element.clientWidth - 100) + radius - 1,
      y: 35 + (bounds.ymax - visualY) / (bounds.ymax - bounds.ymin) * (element.clientHeight - 100) };
  }, turnoverPosition(axis, 100));
  const box = await bubble.boundingBox();
  await page.mouse.move(box!.x + point.x, box!.y + point.y);
  await expect(page.getByText("100.00亿元", { exact: true }).first()).toBeVisible();
  const calendar = page.locator('[data-test="futures-trading-date-picker"]').first();
  await calendar.locator("input").click();
  await page.locator('.futures-trading-calendar:visible [data-day="2026-09-28"]').click();
  await expect(bubble).toHaveAttribute("data-position", "1");
  expect(JSON.parse((await bubble.getAttribute("data-x-scale"))!)).toEqual(axis);
  const before = await bubble.getAttribute("data-view");
  expect(JSON.parse(before!).xmin).toBe(0);
  await bubble.hover();
  await page.mouse.wheel(0, -200);
  await expect.poll(() => bubble.getAttribute("data-view")).not.toBe(before);
  expect(JSON.parse((await bubble.getAttribute("data-view"))!).xmin).toBeGreaterThanOrEqual(0);
  await page.mouse.wheel(0, 3000);
  await expect.poll(async () => JSON.parse((await bubble.getAttribute("data-view"))!).xmin).toBeGreaterThanOrEqual(0);
  const rect = await bubble.boundingBox();
  await page.mouse.move(rect!.x + rect!.width * .5, rect!.y + rect!.height * .5);
  await page.mouse.down();
  await page.mouse.move(rect!.x + rect!.width * .9, rect!.y + rect!.height * .5, { steps: 4 });
  await page.mouse.up();
  expect(JSON.parse((await bubble.getAttribute("data-view"))!).xmin).toBeGreaterThanOrEqual(0);
  await page.getByRole("button", { name: "复位气泡视图" }).click();
  await expect(bubble).toHaveAttribute("data-view", before!);
});

test("calendar marks trading days, synchronizes snapshots and pauses playback without daily requests", async ({ page }) => {
  const requests = await openFutures(page);
  const calendars = page.locator('[data-test="futures-trading-date-picker"]');
  await expect(calendars).toHaveCount(3);
  await calendars.first().locator("input").click();
  const popup = page.locator('.futures-trading-calendar:visible');
  const day29 = popup.locator('[data-day="2026-09-29"]');
  await expect(day29.locator('.trading-day-dot')).toBeVisible();
  const weekend = popup.locator('[data-day="2026-09-27"]');
  await expect(weekend.locator('.trading-day-dot')).toHaveCount(0);
  expect(await weekend.evaluate(node => node.closest("td")!.classList.contains("disabled"))).toBe(true);
  const before = requests.length;
  await day29.click();
  await expect(popup).toBeHidden();
  for (const calendar of await calendars.all()) await expect(calendar.locator("input")).toHaveValue("2026-09-29");
  await expect(page.locator('.timeline-actions time')).toHaveText("2026-09-29");
  await expect(page.locator('[data-test="futures-signed-bar"]').first()).toHaveAttribute("data-positive", "140,70");
  await page.getByRole("button", { name: "播放", exact: true }).click();
  await calendars.nth(2).locator("input").click();
  await expect(page.getByRole("button", { name: "播放", exact: true })).toHaveAttribute("aria-pressed", "false");
  await popup.locator('[data-day="2026-09-24"]').click();
  await expect(popup).toBeHidden();
  await expect(page.getByRole("button", { name: "播放", exact: true })).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator('.timeline-actions time')).toHaveText("2026-09-24");
  await calendars.first().locator("input").click();
  await popup.locator('[data-day="2026-09-28"]').click();
  await expect(popup).toBeHidden();
  await expect(page.getByText("该日没有可显示持仓", { exact: true })).toHaveCount(5);
  await calendars.nth(1).locator("input").click();
  await popup.locator('[data-day="2026-09-30"]').click();
  await expect(popup).toBeHidden();
  await expect(page.getByText("该日没有可显示持仓", { exact: true })).toHaveCount(0);
  expect(requests.length).toBe(before);
});

test("stopped playback and released drag settle on a real trading-day observation", async ({ page }) => {
  await openFutures(page, true);
  const track = page.locator('[data-test="futures-shared-date-track"]');
  const playback = page.locator('[data-test="futures-shared-playback"]');
  const bubble = page.locator('[data-test="futures-bubble"]');
  const bounds = (await track.boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width * .4, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await expect.poll(async () => Number(await playback.getAttribute("data-position"))).toBeCloseTo(1.2, 1);
  await page.mouse.up();
  await expect(playback).toHaveAttribute("data-position", "1");
  await expect(bubble).toHaveAttribute("data-position", "1");
  await expect(bubble).toHaveAttribute("data-incomplete-count", "1");
  await expect(page.locator('.timeline-actions time')).toHaveText("2026-09-28");
  await page.getByRole("button", { name: "播放", exact: true }).click();
  await expect.poll(async () => Number(await playback.getAttribute("data-position"))).toBeGreaterThan(1.1);
  await page.getByRole("button", { name: "暂停", exact: true }).click();
  const position = Number(await playback.getAttribute("data-position"));
  expect(Number.isInteger(position)).toBe(true);
  await expect(bubble).toHaveAttribute("data-position", String(position));
});

test("changing playback range interrupts animation at a real trading day", async ({ page }) => {
  await openFutures(page, true);
  const track = page.locator('[data-test="futures-shared-date-track"]');
  const playback = page.locator('[data-test="futures-shared-playback"]');
  const bubble = page.locator('[data-test="futures-bubble"]');
  const bounds = (await track.boundingBox())!;
  await page.mouse.click(bounds.x + bounds.width / 3, bounds.y + bounds.height / 2);
  await expect(playback).toHaveAttribute("data-position", "1");
  await page.getByRole("button", { name: "播放", exact: true }).click();
  await expect.poll(async () => Number(await playback.getAttribute("data-position"))).toBeGreaterThan(1.1);
  await page.getByRole("combobox", { name: "期货图表日期范围" }).selectOption("20d");
  await expect(page.getByRole("button", { name: "播放", exact: true })).toHaveAttribute("aria-pressed", "false");
  const position = Number(await playback.getAttribute("data-position"));
  expect(Number.isInteger(position)).toBe(true);
  expect(position).toBeGreaterThanOrEqual(0);
  expect(position).toBeLessThanOrEqual(3);
  await expect(bubble).toHaveAttribute("data-position", String(position));
});

test("shorter playback range clamps a selected older date to its first observation", async ({ page }) => {
  const longDates = Array.from({ length: 30 }, (_, i) => `2026-09-${String(i + 1).padStart(2, "0")}`);
  await openFutures(page, false, false, false, false, false, false, false, false, longDates);
  const track = page.locator('[data-test="futures-shared-date-track"]');
  const playback = page.locator('[data-test="futures-shared-playback"]');
  const bounds = (await track.boundingBox())!;
  await page.mouse.click(bounds.x + bounds.width * .04, bounds.y + bounds.height / 2);
  await expect(playback).toHaveAttribute("data-position", "1");
  await page.getByRole("combobox", { name: "期货图表日期范围" }).selectOption("20d");
  await expect(playback).toHaveAttribute("data-position", "10");
  await expect(page.locator('.timeline-actions time')).toHaveText(longDates[10]);
});

test("selected bubble is opaque while other products remain almost transparent", async ({ page }) => {
  await openFutures(page);
  await page.getByRole("combobox", { name: "成交额与涨跌分布选择品种", exact: true }).click();
  await page.getByRole("option", { name: "豆粕（M）", exact: true }).click();
  const bubble = page.locator('[data-test="futures-bubble"]');
  const alphaAt = async (x: number, y: number) => bubble.evaluate((element, point) => {
    const bounds = JSON.parse(element.getAttribute("data-scale")!);
    const canvas = element.querySelector("canvas")!;
    const xi = (bounds.xmax - bounds.xmin) * .08, yi = (bounds.ymax - bounds.ymin) * .1;
    const x = Math.max(bounds.xmin + xi, Math.min(bounds.xmax - xi, point.x));
    const y = Math.max(bounds.ymin + yi, Math.min(bounds.ymax - yi, Math.asinh(point.y / 10)));
    const px = 76 + (x - bounds.xmin) / (bounds.xmax - bounds.xmin) * (element.clientWidth - 100);
    const py = 35 + (bounds.ymax - y) / (bounds.ymax - bounds.ymin) * (element.clientHeight - 100);
    return canvas.getContext("2d")!.getImageData(Math.round(px * canvas.width / element.clientWidth),
      Math.round(py * canvas.height / element.clientHeight), 1, 1).data[3];
  }, { x, y });
  await expect.poll(() => alphaAt(100, 1.5)).toBeGreaterThan(240);
  expect(await alphaAt(30, -1)).toBeLessThan(40);
});

test("底部范围独立控制数据加载，保留公司和品种合约筛选", async ({ page }) => {
  const requests = await openFutures(page);
  await expect(page.locator('.page-heading .el-radio-group')).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "期货公司", exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "期货品种", exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "合约", exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "期货席位", exact: true })).toHaveCount(0);
  const window = page.locator('.playback-dock select').filter({ has: page.locator('option[value="3y"]') });
  await window.selectOption("3y");
  await expect.poll(() => requests.filter(url => url.includes("ranking-series") && url.includes("range=3y")).length).toBeGreaterThan(0);
  await expect(page.locator('[data-test="futures-signed-bar"]')).toHaveCount(5);
  const before = requests.length;
  await window.selectOption("60d");
  await expect.poll(() => requests.slice(before).filter(url => url.includes("ranking-series") && url.includes("range=1y")).length).toBeGreaterThan(0);
});
