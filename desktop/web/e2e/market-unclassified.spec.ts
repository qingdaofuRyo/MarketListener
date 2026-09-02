import { expect, test } from "@playwright/test";


test("market page omits the internal unclassified review section", async ({ page }) => {
  const dataVersion = "unclassified-v3";
  let unclassifiedRequested = false;
  await page.route("**/api/strategy/definitions*", route => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/market/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/market/cache-status") return route.fulfill({ json: { dataVersion } });
    if (path === "/api/market/categories") return route.fulfill({ json: { items: [{ id: "all", label: "全部市场" }], total: 1 } });
    if (path === "/api/market/instruments") return route.fulfill({ json: { items: [], total: 0, dataVersion } });
    if (path === "/api/market/unclassified") { unclassifiedRequested = true; return route.fulfill({ json: { items: [], total: 0 } }); }
    return route.fulfill({ json: { bars: [], total: 0, start: 0, size: 0, hasMore: false, availablePeriods: [] } });
  });

  await page.goto("/market/");
  await expect(page.getByRole("heading", { name: "待分类标的" })).toHaveCount(0);
  expect(unclassifiedRequested).toBe(false);

  await page.locator(".all-toolbar .el-select").click();
  await expect(page.getByRole("option", { name: "其它", exact: true })).toHaveCount(0);
});

test("market list ignores a persisted pre-v3 category response", async ({ page }) => {
  const revision = "fixture-r4";
  const staleVersion = `${revision}:market-categories-r4-v1`;
  const currentVersion = `${revision}:market-categories-r4-v3`;
  const staleKey = `/api/market/instruments?page=1&pageSize=20&version=${encodeURIComponent(staleVersion)}`;
  const staleInstrument = {
    instrumentId: "GLOBAL.UNKNOWN.LEGACY",
    symbol: "LEGACY",
    name: "旧版待分类标的",
    market: "GLOBAL",
    assetType: "UNKNOWN",
  };
  const requestedVersions: Array<string | null> = [];

  // Establish an origin, then seed the real persistent-cache store before
  // mounting Vue.  This models an existing user's browser after R4-T010.
  await page.goto("/favicon.ico");
  await page.evaluate(async ({ key, value }) => {
    await new Promise<void>((resolve, reject) => {
      const openRequest = indexedDB.open("marketlistener-query-cache", 1);
      openRequest.onupgradeneeded = () => {
        if (!openRequest.result.objectStoreNames.contains("queries")) openRequest.result.createObjectStore("queries", { keyPath: "key" });
      };
      openRequest.onerror = () => reject(openRequest.error);
      openRequest.onsuccess = () => {
        const transaction = openRequest.result.transaction("queries", "readwrite");
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error);
        transaction.objectStore("queries").put({ key, value, savedAt: Date.now() });
      };
    });
  }, { key: staleKey, value: { items: [staleInstrument], total: 1, dataVersion: staleVersion } });

  await page.route("**/api/strategy/definitions*", route => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/market/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/market/cache-status") return route.fulfill({ json: { dataVersion: currentVersion } });
    if (url.pathname === "/api/market/categories") return route.fulfill({ json: { items: [{ id: "all", label: "全部市场" }], total: 1 } });
    if (url.pathname === "/api/market/unclassified") return route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 50 } });
    if (url.pathname === "/api/market/instruments") {
      requestedVersions.push(url.searchParams.get("version"));
      return route.fulfill({ json: {
        items: [{ instrumentId: "CN.SSE.STOCK.600000", symbol: "600000", name: "浦发银行", market: "CN", assetType: "STOCK" }],
        total: 1,
        dataVersion: currentVersion,
      } });
    }
    return route.fulfill({ json: { bars: [], total: 0, start: 0, size: 0, hasMore: false, availablePeriods: [], dataVersion: currentVersion } });
  });

  await page.goto("/market/");
  await expect(page.locator(".instrument-row").first()).toContainText("浦发银行");
  await expect(page.locator(".all-section")).not.toContainText("旧版待分类标的");
  expect(requestedVersions).toEqual([currentVersion]);
});
