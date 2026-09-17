import { test, expect } from "@playwright/test";
import { createPinia, setActivePinia } from "pinia";
import { useTargetMarketStore } from "../src/stores/targetMarket";

test("composite store rejects stale loads/errors and preserves scan filters", async () => {
  const originalFetch = globalThis.fetch;
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { origin: "http://localhost" } } });
  type Pending = { body: Record<string, unknown>; resolve: (response: Response) => void; reject: (reason: Error) => void };
  const pending: Pending[] = [];
  globalThis.fetch = ((_url, init) => new Promise<Response>((resolve, reject) => {
    pending.push({ body: init?.body ? JSON.parse(String(init.body)) : {}, resolve, reject });
  })) as typeof fetch;
  setActivePinia(createPinia());
  const store = useTargetMarketStore();
  const response = (name: string, extra = {}) => Response.json({ items: [{ instrumentId: name }], events: [], ...extra });
  try {
    const loading = store.load();
    const scan = store.scan();
    expect(pending).toHaveLength(3);
    pending[2].resolve(response("new")); await scan;
    pending[0].resolve(Response.json({ items: [] })); pending[1].resolve(response("stale")); await loading;
    expect(store.monitored[0].instrumentId).toBe("new");

    const oldScan = store.scan(); store.stopScan();
    store.selectedStrategies = ["original"]; store.selectedMarkets = ["cn-future-main"];
    const newScan = store.scan();
    pending[3].reject(new Error("obsolete failure")); await oldScan;
    expect(store.error).toBe(""); expect(store.loading).toBe(true);
    store.selectedStrategies = ["changed"]; store.selectedMarkets = ["hk-stock"];
    pending[4].resolve(response("page1", { nextAfterId: "next" }));
    await expect.poll(() => pending.length).toBe(6);
    expect(pending[5].body.strategyIds).toEqual(["original"]);
    expect(pending[5].body.categoryKeys).toEqual(["cn-future-main"]);
    pending[5].resolve(response("page2")); await newScan;
    expect(store.monitored[0].instrumentId).toBe("page2"); expect(store.loading).toBe(false);
    const finalScan = store.scan();
    const concurrentLoad = store.load();
    pending[6].resolve(response("final")); await finalScan;
    pending[7].resolve(Response.json({ items: [] })); pending[8].resolve(response("older-load")); await concurrentLoad;
    expect(store.monitored[0].instrumentId).toBe("final");
  } finally {
    store.$dispose(); globalThis.fetch = originalFetch;
    if (windowDescriptor) Object.defineProperty(globalThis, "window", windowDescriptor);
    else Reflect.deleteProperty(globalThis, "window");
  }
});
