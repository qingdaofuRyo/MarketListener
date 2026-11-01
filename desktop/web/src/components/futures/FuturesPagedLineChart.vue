<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";
import type { RankingSeries } from "../../domain/futures";

const props = withDefaults(defineProps<{ dates: string[]; series: RankingSeries[]; title: string; unit?: string; area?: boolean }>(), { unit: "张", area: false });
const theme = useThemeStore();
const element = ref<HTMLElement>();
const page = ref(1);
const pages = computed(() => props.series.length ? Math.max(1, Math.ceil(props.series.length / 20)) : Math.max(1, page.value));
const visible = computed(() => props.series.slice((page.value - 1) * 20, page.value * 20));
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;
function color(key: string): string {
  let hash = 0;
  for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `hsl(${hash % 360}, 65%, 52%)`;
}
function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  chart.setOption({
    animation: false, backgroundColor: "transparent", aria: { enabled: true },
    tooltip: { trigger: "axis", confine: true, backgroundColor: palette.chartTooltip, borderColor: palette.chartTooltipBorder, textStyle: { color: palette.textPrimary } },
    legend: { type: "scroll", top: 0, textStyle: { color: palette.textSecondary } },
    grid: { left: 75, right: 20, top: 52, bottom: 55 },
    xAxis: { type: "category", boundaryGap: false, data: props.dates, axisLabel: { color: palette.chartAxis, hideOverlap: true }, axisLine: { lineStyle: { color: palette.chartGrid } } },
    yAxis: { type: "value", name: props.unit, nameTextStyle: { color: palette.chartAxis }, axisLabel: { color: palette.chartAxis }, splitLine: { lineStyle: { color: palette.chartGrid } } },
    dataZoom: [{ type: "inside", filterMode: "none" }, { type: "slider", height: 16, bottom: 4, filterMode: "none" }],
    series: visible.value.map(item => ({ id: item.key, name: item.name, type: "line", data: item.values, showSymbol: false, connectNulls: false, lineStyle: { width: 1.6, color: color(item.key) }, itemStyle: { color: color(item.key) }, areaStyle: props.area ? { opacity: 0.22 } : undefined })),
  }, { notMerge: true });
}
watch(() => props.series, () => { if (props.series.length) page.value = Math.min(page.value, pages.value); });
watch(() => [props.dates, props.series, page.value, theme.palette], () => void nextTick(render));
onMounted(() => { void nextTick(render); observer = new ResizeObserver(() => chart?.resize()); if (element.value) observer.observe(element.value); });
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose(); });
</script>

<template>
  <article class="line-card">
    <header><h3>{{ title }}</h3><div v-if="!area" class="pagination" data-test="line-pagination"><button :disabled="page <= 1" @click="page--">上一页</button><span>{{ page }} / {{ pages }}</span><button :disabled="page >= pages" @click="page++">下一页</button></div></header>
    <div ref="element" class="line-chart" :data-test="area ? 'futures-structure-chart' : 'futures-paged-line-chart'" :data-series-count="visible.length" :data-series-keys="visible.map(item => item.key).join(',')" role="img" :aria-label="title" />
  </article>
</template>

<style scoped>
.line-card { min-width: 0; margin-top: 14px; }
header,.pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h3 { margin: 0; font-size: 14px; }
.line-chart { width: 100%; height: 430px; }
button { padding: 4px 10px; color: var(--ml-text-primary); background: var(--ml-surface-elevated); border: 1px solid var(--ml-divider); border-radius: 4px; cursor: pointer; }
button:disabled { opacity: .4; cursor: default; }
</style>
