<script setup lang="ts">
import * as echarts from "echarts";
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { FuturesStructureResponse } from "../../domain/futures";
import { useThemeStore } from "../../stores/theme";

const props = withDefaults(defineProps<{
  payload: FuturesStructureResponse;
  axisName?: string;
  ariaLabel?: string;
}>(), {
  axisName: "单边持仓（张）",
  ariaLabel: "中国商品期货固定顺序堆叠面积图",
});

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;
const colors = [
  "#2563eb", "#d946ef", "#16a34a", "#ea580c", "#0891b2", "#7c3aed", "#dc2626", "#65a30d",
  "#0284c7", "#c026d3", "#ca8a04", "#0f766e", "#4f46e5", "#be123c", "#15803d", "#9333ea",
];

function formatValue(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char] || char));
}

function tooltipHtml(params: unknown): string {
  const items = Array.isArray(params) ? params as Array<{ axisValue?: string; seriesName?: string; value?: number | null; color?: string }> : [];
  const index = props.payload.dates.indexOf(String(items[0]?.axisValue || ""));
  if (index < 0) return "";
  const coverage = props.payload.coverage[index];
  const rows = items
    .filter((item) => item.value !== null && item.value !== undefined)
    .map((item) => `<div class="structure-tooltip-row"><span><i style="background:${item.color}"></i>${escapeHtml(item.seriesName)}</span><strong>${formatValue(item.value)}</strong></div>`)
    .join("");
  return `<div class="structure-tooltip"><strong>${escapeHtml(props.payload.dates[index])}</strong>${rows}`
    + `<hr><div class="structure-tooltip-row"><span>市场总持仓</span><strong>${formatValue(props.payload.totals[index])}</strong></div>`
    + `<div class="structure-tooltip-row"><span>未分类新品种</span><strong>${formatValue(props.payload.unclassifiedTotals[index])}</strong></div>`
    + `<div class="structure-tooltip-row"><span>输入覆盖</span><strong>${coverage ? `${coverage.inputRowCount - coverage.missingRowCount}/${coverage.inputRowCount}` : "—"}</strong></div></div>`;
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  const useSampling = props.payload.dates.length > 1500;
  chart.setOption({
    animationDuration: 220,
    animationDurationUpdate: 120,
    backgroundColor: "transparent",
    aria: { enabled: true, decal: { show: false } },
    color: colors,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: palette.textDisabled } },
      backgroundColor: palette.chartTooltip,
      borderColor: palette.chartTooltipBorder,
      textStyle: { color: palette.textPrimary, fontSize: 12 },
      confine: true,
      formatter: tooltipHtml,
    },
    legend: {
      type: "scroll",
      data: props.payload.series.map((item) => item.memberName),
      top: 0,
      right: 8,
      itemWidth: 14,
      itemHeight: 8,
      textStyle: { color: palette.textSecondary, fontSize: 11 },
    },
    grid: { left: 64, right: 18, top: 46, bottom: 48 },
    xAxis: {
      type: "category",
      data: props.payload.dates,
      boundaryGap: false,
      axisLine: { lineStyle: { color: palette.chartGrid } },
      axisLabel: { color: palette.chartAxis, fontSize: 11, hideOverlap: true },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      name: props.axisName,
      nameTextStyle: { color: palette.chartAxis, fontSize: 11, padding: [0, 0, 0, 2] },
      axisLabel: { color: palette.chartAxis, fontSize: 11, formatter: (value: number) => formatValue(value) },
      splitLine: { lineStyle: { color: palette.chartGrid } },
    },
    dataZoom: [
      { type: "inside", start: 0, end: 100, filterMode: "none" },
      { type: "slider", start: 0, end: 100, bottom: 4, height: 14, borderColor: palette.divider, backgroundColor: palette.surfaceElevated, fillerColor: `${palette.accent}26`, handleStyle: { color: palette.accent }, textStyle: { color: palette.chartAxis }, filterMode: "none" },
    ],
    series: props.payload.series.map((item, index) => ({
      id: item.memberKey,
      name: item.memberName,
      type: "line" as const,
      stack: "open-interest",
      areaStyle: { opacity: 0.72 },
      data: item.values,
      showSymbol: false,
      connectNulls: false,
      sampling: useSampling ? "lttb" : undefined,
      lineStyle: { width: 1, color: colors[index % colors.length] },
      itemStyle: { color: colors[index % colors.length] },
      emphasis: { focus: "series" as const },
      blur: { lineStyle: { opacity: 0.18 }, areaStyle: { opacity: 0.14 } },
    })),
  }, true);
}

function resize(): void { chart?.resize(); }

watch(() => [props.payload, theme.palette], () => void nextTick(render), { deep: true });
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
});
</script>

<template>
  <div ref="element" class="structure-chart" data-test="futures-structure-chart" role="img" :aria-label="ariaLabel" />
</template>

<style scoped>
.structure-chart { height: 430px; width: 100%; }
</style>

<style>
.structure-tooltip { min-width: 225px; }
.structure-tooltip-row { display: flex; justify-content: space-between; gap: 22px; line-height: 1.8; }
.structure-tooltip-row i { display: inline-block; width: 7px; height: 7px; margin-right: 6px; border-radius: 50%; }
.structure-tooltip hr { border: 0; border-top: 1px solid rgba(127, 127, 127, .35); margin: 6px 0; }
</style>
