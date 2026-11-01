<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { RankingSeries } from "../../domain/futures";
import { useThemeStore } from "../../stores/theme";
import { futuresAxisPointer, futuresLegend, futuresTooltip, futuresZoom } from "../../domain/futuresChartDesign";

const props = withDefaults(defineProps<{ dates: string[]; series: RankingSeries[]; title: string; unit?: string; area?: boolean }>(), { unit: "张", area: false });
const theme = useThemeStore();
const element = ref<HTMLElement>();
const hasData = computed(() => props.series.some(item => item.values.some(value => typeof value === "number" && Number.isFinite(value))));
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;
function color(key: string): string {
  if (key.endsWith(":long")) return "#dc2626";
  if (key.endsWith(":short")) return "#16a34a";
  let hash = 0; for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `hsl(${hash % 360},65%,52%)`;
}
function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  if (!hasData.value) { chart.clear(); return; }
  const p = theme.palette;
  chart.setOption({
    animation: false, aria: { enabled: true }, backgroundColor: "transparent",
    tooltip: { ...futuresTooltip(p), trigger: "axis", axisPointer: futuresAxisPointer(p) },
    legend: futuresLegend(p),
    grid: { left: 76, right: 20, top: 46, bottom: 50 },
    xAxis: { type: "category", data: props.dates, boundaryGap: false, axisLabel: { color: p.chartAxis, hideOverlap: true }, axisLine: { lineStyle: { color: p.chartGrid } } },
    yAxis: { type: "value", name: props.unit, min: props.unit === "热度" ? -100 : undefined,
      max: props.unit === "热度" ? 100 : undefined,
      nameTextStyle: { color: p.chartAxis }, axisLabel: { color: p.chartAxis },
      splitLine: { lineStyle: { color: p.chartGrid } } },
    dataZoom: futuresZoom(p),
    series: props.series.map(item => ({ id: item.key, name: item.name, type: "line", data: item.values,
      showSymbol: false, symbol: "circle", symbolSize: 5, connectNulls: false,
      lineStyle: { color: color(item.key), width: 2 }, itemStyle: { color: color(item.key) },
      emphasis: { focus: "series", lineStyle: { width: 3 }, itemStyle: { borderColor: p.surface, borderWidth: 2 } },
      areaStyle: props.area ? { opacity: .2 } : undefined })),
  }, true);
}
watch(() => [props.dates, props.series, props.area, theme.palette], () => void nextTick(render), { deep: true });
onMounted(() => { void nextTick(render); observer = new ResizeObserver(() => chart?.resize()); if (element.value) observer.observe(element.value); });
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose(); });
</script>
<template>
  <article class="timeline-card"><h3>{{ title }}</h3>
    <div ref="element" class="timeline" data-test="futures-timeline" :data-area="area" :data-series-count="series.length" role="img" :aria-label="title" />
    <div v-if="!hasData" class="empty">{{ series.length ? '所选范围暂无可用序列' : '选择品种或席位查看历史' }}</div>
  </article>
</template>
<style scoped>
.timeline-card { position: relative; min-width: 0; margin-top: 16px; padding: 14px 10px 8px; border: 1px solid var(--ml-divider); border-radius: 13px; background: var(--ml-surface-elevated); transition: border-color 180ms, box-shadow 180ms; }
.timeline-card:hover { border-color: color-mix(in srgb, var(--ml-accent) 30%, var(--ml-divider)); box-shadow: 0 5px 18px rgba(25,40,75,.05); }
h3 { font-size: 14px; margin: 0 0 6px; }.timeline { height: 330px; width: 100%; }.empty { position: absolute; inset: 50px 0 0; display: grid; place-items: center; pointer-events: none; color: var(--ml-text-secondary); }
</style>
