<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { buildSpreadRegions } from "../../domain/futuresSpreadArea";
import { useThemeStore } from "../../stores/theme";
import { futuresAxisPointer, futuresLegend, futuresTooltip, futuresZoom } from "../../domain/futuresChartDesign";
import FuturesLoadingSkeleton from "./FuturesLoadingSkeleton.vue";

const props = defineProps<{
  dates: string[];
  longValues: Array<number | null>;
  shortValues: Array<number | null>;
  title: string;
  longLabel: string;
  shortLabel: string;
  loading?: boolean;
}>();
const theme = useThemeStore();
const element = ref<HTMLElement>();
const regions = computed(() => buildSpreadRegions(props.longValues, props.shortValues));
const hasData = computed(() => [...props.longValues, ...props.shortValues]
  .some(value => typeof value === "number" && Number.isFinite(value)));
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;

function display(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "未公布" : `${value.toLocaleString("zh-CN")} 张`;
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  if (!hasData.value) { chart.clear(); return; }
  const palette = theme.palette;
  const polygons = regions.value;
  chart.setOption({
    animation: false, aria: { enabled: true }, backgroundColor: "transparent",
    tooltip: { ...futuresTooltip(palette), trigger: "axis", axisPointer: futuresAxisPointer(palette),
      formatter: (params: unknown) => {
        const items = Array.isArray(params) ? params as Array<{ value?: number[]; seriesId?: string; axisValue?: number }> : [];
        const point = items.find(item => item.seriesId === "long-position" || item.seriesId === "short-position");
        const position = Math.round(point?.axisValue ?? point?.value?.[0] ?? 0);
        const day = props.dates[position] || "";
        return `${day}<br>${props.longLabel}：${display(props.longValues[position])}<br>${props.shortLabel}：${display(props.shortValues[position])}`;
      } },
    legend: { ...futuresLegend(palette), data: [props.longLabel, props.shortLabel] },
    grid: { left: 78, right: 26, top: 46, bottom: 52 },
    xAxis: { type: "value", min: 0, max: Math.max(1, props.dates.length - 1), interval: Math.max(1, Math.ceil(props.dates.length / 7)),
      axisLabel: { color: palette.chartAxis, formatter: (value: number) => props.dates[Math.round(value)] || "" },
      axisPointer: { label: { formatter: (params: { value: number }) => props.dates[Math.round(params.value)] || "" } },
      axisLine: { lineStyle: { color: palette.chartGrid } }, splitLine: { show: false } },
    yAxis: { type: "value", name: "张", min: 0, nameTextStyle: { color: palette.chartAxis },
      axisLabel: { color: palette.chartAxis, formatter: (value: number) => value.toLocaleString("zh-CN") },
      splitLine: { lineStyle: { color: palette.chartGrid } } },
    dataZoom: futuresZoom(palette),
    series: [
      { id: "position-spread", name: "方向差", type: "custom", silent: true, tooltip: { show: false },
        data: polygons.map((_, index) => [index]),
        renderItem: (params: { dataIndex: number }, api: { coord: (value: number[]) => number[] }) => {
          const region = polygons[params.dataIndex];
          return { type: "polygon", shape: { points: region.points.map(point => api.coord(point)) },
            style: { fill: region.side === "long" ? "rgba(220,38,38,.28)" : "rgba(22,163,74,.28)", stroke: "none" } };
        }, z: 1 },
      { id: "long-position", name: props.longLabel, type: "line", showSymbol: false, connectNulls: false,
        data: props.dates.map((_, index) => [index, props.longValues[index] ?? null]),
        lineStyle: { color: palette.priceUp, width: 2.2 }, itemStyle: { color: palette.priceUp },
        emphasis: { focus: "series", lineStyle: { width: 3.4 } }, z: 3 },
      { id: "short-position", name: props.shortLabel, type: "line", showSymbol: false, connectNulls: false,
        data: props.dates.map((_, index) => [index, props.shortValues[index] ?? null]),
        lineStyle: { color: palette.priceDown, width: 2.2 }, itemStyle: { color: palette.priceDown },
        emphasis: { focus: "series", lineStyle: { width: 3.4 } }, z: 3 },
    ],
  }, true);
}

watch(() => [props.dates, props.longValues, props.shortValues, props.longLabel, props.shortLabel, theme.palette],
  () => void nextTick(render), { deep: true });
onMounted(() => { void nextTick(render); observer = new ResizeObserver(() => chart?.resize()); if (element.value) observer.observe(element.value); });
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose(); });
</script>

<template>
  <article class="spread-chart">
    <h3>{{ title }}</h3>
    <div ref="element" class="chart" data-test="futures-position-spread-area"
      :data-region-count="regions.length" :data-red-regions="regions.filter(item => item.side === 'long').length"
      :data-green-regions="regions.filter(item => item.side === 'short').length"
      role="img" :aria-label="title" />
    <div v-if="loading && !hasData" class="spread-loading-skeleton"><FuturesLoadingSkeleton kind="overview" /></div>
    <div v-else-if="!hasData" class="empty">选择席位或品种查看历史</div>
  </article>
</template>

<style scoped>
.spread-chart { position: relative; min-width: 0; margin-top: 16px; padding: 14px 12px 8px; border: 1px solid var(--ml-divider); border-radius: 10px; background: var(--ml-surface-elevated); transition: border-color 180ms; }
.spread-chart:hover { border-color: color-mix(in srgb, var(--ml-accent) 18%, var(--ml-divider)); }
h3 { display: flex; align-items: center; min-height: 32px; padding: 0 2px 10px; font-size: 13px; line-height: 1.6; font-weight: 600; margin: 0 0 10px; border-bottom: 1px solid var(--ml-divider); overflow-wrap: anywhere; }
.chart { width: 100%; height: 330px; }
.spread-loading-skeleton { position: absolute; inset: 62px 12px 8px; overflow: hidden; background: var(--ml-surface-elevated); }
.spread-loading-skeleton :deep(.futures-skeleton) { display: flex; flex-direction: column; height: 100%; }
.spread-loading-skeleton :deep(.plot-overview) { flex: 1; min-height: 0; }
.empty { position: absolute; inset: 62px 0 0; display: grid; place-items: center; pointer-events: none; color: var(--ml-text-secondary); }
@media (prefers-reduced-motion: reduce) { .spread-chart { transition: none; } }
</style>
