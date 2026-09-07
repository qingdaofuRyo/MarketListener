import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { apiGet, apiPost } from "../domain/api";

interface Strategy { strategyId: string; displayName: string; }
interface SignalEvent { instrumentId: string; strategyName: string; action: string; direction: string; at: string; }
interface MonitorItem { instrumentId: string; symbol?: string; name?: string; latestPrice?: number; lastClose?: number; direction: string; openedAt: string; openingStrategyId: string; latestSignal?: SignalEvent; }
interface MonitorResponse { items: MonitorItem[]; events: SignalEvent[]; nextOffset?: number | null; nextAfterId?: string | null; scanned?: number; total?: number; issueCount?: number; }

/** Signal monitoring belongs to the domain service, not to one routed page. */
export const useTargetMarketStore = defineStore("targetMarket", () => {
  const strategies = ref<Strategy[]>([]);
  const monitored = ref<MonitorItem[]>([]);
  const events = ref<SignalEvent[]>([]);
  const selectedMarkets = ref<string[]>([]);
  const selectedStrategies = ref<string[]>([]);
  const loading = ref(false);
  const error = ref("");
  const progress = ref("");
  let serial = 0;
  let monitorTimer: ReturnType<typeof setInterval> | undefined;

  const filtered = computed(() => monitored.value.filter((item) =>
    !selectedStrategies.value.length || selectedStrategies.value.includes(item.openingStrategyId),
  ));

  async function load(): Promise<void> {
    try {
      const [definitions, monitor] = await Promise.all([
        apiGet<{ items: Array<{ id: string; displayName: string; action: string; enabled: boolean }> }>("/api/signals/definitions", undefined, { force: true }),
        apiGet<MonitorResponse>("/api/signals/monitor", undefined, { force: true }),
      ]);
      strategies.value = definitions.items.filter((item) => item.action === "open" && item.enabled).map((item) => ({ strategyId: item.id, displayName: item.displayName }));
      monitored.value = monitor.items;
      events.value = monitor.events;
    } catch (reason) { error.value = reason instanceof Error ? reason.message : "目标行情加载失败"; }
  }
  async function scan(monitoringOnly = false): Promise<void> {
    if (loading.value) return;
    const request = ++serial; loading.value = true; error.value = "";
    let offset: number | null = 0; let afterId: string | undefined; let scanned = 0; let issues = 0;
    try {
      while (offset !== null && request === serial) {
        const response: MonitorResponse = await apiPost<MonitorResponse>("/api/signals/scan", { strategyIds: selectedStrategies.value, categoryKeys: selectedMarkets.value, offset, afterId, limit: 40, monitoringOnly });
        if (request !== serial) return;
        scanned += response.scanned || 0; issues += response.issueCount || 0;
        monitored.value = response.items; events.value = response.events;
        progress.value = `已检查 ${scanned} / ${response.total || 0} 个标的；${issues} 项字段/暖机不可用`;
        if (response.nextAfterId) { afterId = response.nextAfterId; offset = 0; } else offset = response.nextOffset ?? null;
      }
    } catch (reason) { error.value = reason instanceof Error ? reason.message : "信号扫描失败"; }
    finally { if (request === serial) loading.value = false; }
  }
  function toggle(list: string[], value: string): string[] { return list.includes(value) ? list.filter((item) => item !== value) : [...list, value]; }
  function toggleMarket(value: string): void { selectedMarkets.value = value === "all" ? [] : toggle(selectedMarkets.value, value); void load(); }
  function toggleStrategy(value: string): void { selectedStrategies.value = value === "all" ? [] : toggle(selectedStrategies.value, value); void load(); }
  function start(): void { if (monitorTimer) return; void load(); monitorTimer = setInterval(() => { if (monitored.value.length) void scan(true); }, 30_000); }
  function stopScan(): void { serial++; }
  return { strategies, monitored, events, selectedMarkets, selectedStrategies, filtered, loading, error, progress, load, scan, toggleMarket, toggleStrategy, start, stopScan };
});
