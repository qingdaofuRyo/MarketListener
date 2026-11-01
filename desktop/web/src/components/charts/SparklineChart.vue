<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";

export interface SparklinePoint {
  t: string;
  value: number | null;
}

const props = withDefaults(defineProps<{
  points: SparklinePoint[];
  height?: number;
  tone?: "neutral" | "up" | "down";
}>(), { points: () => [], height: 82, tone: "neutral" });

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;

const orderedPoints = computed(() => [...props.points]
  .filter(point => typeof point.t === "string" && point.t.length > 0)
  .sort((left, right) => left.t.localeCompare(right.t))
  .map(point => ({ t: point.t, value: typeof point.value === "number" && Number.isFinite(point.value) ? point.value : null })));
const observedValues = computed(() => orderedPoints.value.flatMap(point => point.value === null ? [] : [point.value]));
const hasData = computed(() => observedValues.value.length > 0);
const bounds = computed(() => {
  let min = Infinity;
  let max = -Infinity;
  for (const value of observedValues.value) {
    min = Math.min(min, value);
    max = Math.max(max, value);
  }
  const padding = (max - min || Math.max(Math.abs(max), 1)) * 0.12;
  return { min: min - padding, max: max + padding };
});
const accessibleSummary = computed(() => {
  if (!hasData.value) return "历史走势：暂无观测数据";
  const first = orderedPoints.value.find(point => point.value !== null);
  const last = [...orderedPoints.value].reverse().find(point => point.value !== null);
  const gaps = orderedPoints.value.some(point => point.value === null) ? "；缺报处断线" : "";
  return `历史走势：${observedValues.value.length} 个观测，${first?.t} ${first?.value}，至 ${last?.t} ${last?.value}${gaps}`;
});

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char] || char));
}

function render(): void {
  if (!element.value) return;
  if (!hasData.value) { chart?.clear(); return; }
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  const color = props.tone === "up" ? palette.priceUp : props.tone === "down" ? palette.priceDown : palette.accent;
  chart.setOption({
    backgroundColor: "transparent",
    animationDuration: 200,
    // Keep the component's explicit summary: ECharts' generated description
    // renders missing points as NaN and overrides the Vue aria-label.
    aria: { enabled: false },
    tooltip: {
      trigger: "axis",
      formatter: (params: Array<{ axisValue: string; value: number | null }>) => {
        const point = params[0];
        if (!point) return "";
        const value = typeof point.value === "number" && Number.isFinite(point.value)
          ? point.value.toLocaleString("zh-CN") : "缺报";
        return `${escapeHtml(point.axisValue)}：${value}`;
      },
      backgroundColor: palette.chartTooltip,
      borderColor: palette.chartTooltipBorder,
      textStyle: { color: palette.textPrimary, fontSize: 11 },
      confine: true,
    },
    grid: { left: 3, right: 3, top: 6, bottom: 3 },
    xAxis: { type: "category", data: orderedPoints.value.map(point => point.t), boundaryGap: false, show: false },
    yAxis: { type: "value", min: bounds.value.min, max: bounds.value.max, show: false },
    series: [{
      type: "line", data: orderedPoints.value.map(point => point.value),
      smooth: false, showSymbol: false, connectNulls: false,
      lineStyle: { color, width: 2 },
      itemStyle: { color },
      areaStyle: { color, opacity: 0.14 },
    }],
  }, true);
}

function resize(): void { chart?.resize(); }
watch(() => [props.points, props.tone, theme.palette], () => void nextTick(render), { deep: true });
onMounted(() => {
  void nextTick(render);
  window.addEventListener("resize", resize);
  if (element.value && typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(element.value);
  }
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize);
  resizeObserver?.disconnect();
  chart?.dispose();
  chart = undefined;
});
</script>

<template>
  <div class="sparkline-chart" :style="{ height: `${height}px` }" data-test="sparkline-chart">
    <div ref="element" class="chart-root" role="img" :aria-label="accessibleSummary" />
    <div v-if="!hasData" class="chart-empty" data-test="sparkline-empty">暂无历史数据</div>
  </div>
</template>

<style scoped>
.sparkline-chart { position: relative; width: 100%; min-width: 0; }
.chart-root { width: 100%; height: 100%; }
.chart-empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--ml-text-secondary); font-size: 12px; background: var(--ml-surface); }
</style>
