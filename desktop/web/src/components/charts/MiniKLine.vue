<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { apiGet } from "../../domain/api";
import KLineChart, { type ChartDrawing, type KLineBar } from "./KLineChart.vue";

type Bar = KLineBar;
interface BatchBars { items: Record<string, Bar[]>; }

const props = withDefaults(defineProps<{ instrumentId: string; period?: string; limit?: number; dataVersion?: string; drawings?: ChartDrawing[]; totalMarketCap?: number; floatMarketCap?: number; futureUnits?: boolean }>(), { period: "1d", limit: 60, dataVersion: "", drawings: () => [], totalMarketCap: undefined, floatMarketCap: undefined, futureUnits: false });
const root = ref<HTMLElement>(); const bars = ref<Bar[]>([]); const loading = ref(false); const visible = ref(false);
let observer: IntersectionObserver | undefined;
const hotBars = new Map<string, Bar[]>();
const pending = new Map<string, Array<{ instrumentId: string; resolve: (bars: Bar[]) => void; reject: (reason: unknown) => void }>>();
let batchTimer: ReturnType<typeof setTimeout> | undefined;

function cacheKey(instrumentId: string, period: string, limit: number, dataVersion: string): string { return `${dataVersion}|${period}|${limit}|${instrumentId}`; }
function groupKey(period: string, limit: number, dataVersion: string): string { return `${dataVersion}|${period}|${limit}`; }
function queueBars(instrumentId: string, period: string, limit: number, dataVersion: string): Promise<Bar[]> {
  const key = cacheKey(instrumentId, period, limit, dataVersion); const cached = hotBars.get(key); if (cached) return Promise.resolve(cached);
  const group = groupKey(period, limit, dataVersion);
  const result = new Promise<Bar[]>((resolve, reject) => { const rows = pending.get(group) ?? []; rows.push({ instrumentId, resolve, reject }); pending.set(group, rows); });
  if (!batchTimer) batchTimer = setTimeout(() => { void flushBatches(); }, 24);
  return result;
}
async function flushBatches(): Promise<void> {
  batchTimer = undefined; const groups = [...pending.entries()]; pending.clear();
  await Promise.all(groups.map(async ([group, requests]) => {
    const [dataVersion, period, limitText] = group.split("|"); const limit = Number(limitText);
    for (let offset = 0; offset < requests.length; offset += 48) {
      const chunk = requests.slice(offset, offset + 48); const ids = [...new Set(chunk.map((item) => item.instrumentId))];
      try {
        const response = await apiGet<BatchBars>("/api/market/instruments/bars/batch", { instrumentIds: ids.join(","), period, limit, version: dataVersion || undefined }, { ttlMs: 5 * 60_000, persist: true });
        for (const request of chunk) { const value = response.items[request.instrumentId] ?? []; hotBars.set(cacheKey(request.instrumentId, period, limit, dataVersion), value); request.resolve(value); }
      } catch (reason) { for (const request of chunk) request.reject(reason); }
    }
  }));
}

async function load(): Promise<void> { if (!visible.value || loading.value || bars.value.length) return; loading.value = true; try { bars.value = await queueBars(props.instrumentId, props.period, props.limit, props.dataVersion); } catch { bars.value = []; } finally { loading.value = false; } }
watch(() => props.dataVersion, () => { bars.value = []; void load(); });
onMounted(() => { if (!root.value || !('IntersectionObserver' in window)) { visible.value = true; void load(); return; } observer = new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) { visible.value = true; observer?.disconnect(); void load(); } }, { rootMargin: "300px" }); observer.observe(root.value); });
onBeforeUnmount(() => observer?.disconnect());
</script>

<template>
  <div ref="root" class="mini-kline" aria-label="最近 60 根交互 K 线图" @click.stop>
    <KLineChart v-if="bars.length" :bars="bars" :period="period" :height="300" :default-visible="40" :drawings="drawings" :total-market-cap="totalMarketCap" :float-market-cap="floatMarketCap" :future-units="futureUnits" compact show-quote-panel drawings-read-only />
    <span v-else-if="loading">加载 K 线…</span><span v-else>暂无 K 线数据</span>
  </div>
</template>

<style scoped>
.mini-kline{height:300px;border-top:1px solid var(--ml-divider);background:linear-gradient(180deg,transparent,var(--ml-background));color:var(--ml-text-disabled);font-size:11px;display:grid;place-items:center}.mini-kline :deep(.kline-chart){width:100%}
</style>
