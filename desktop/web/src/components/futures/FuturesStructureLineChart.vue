<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { FuturesStructureResponse } from "../../domain/futures";
import { useThemeStore } from "../../stores/theme";

const props = withDefaults(defineProps<{
  payload: FuturesStructureResponse;
  axisName?: string;
  ariaLabel?: string;
}>(), {
  axisName: "日度值",
  ariaLabel: "国内期货品种日度变化折线图",
});

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;
const lineScope = ref<"all" | "area">("all");
const displayedSeries = computed(() => lineScope.value === "all" ? (props.payload.lineSeries ?? props.payload.series) : props.payload.series);
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
  const items = Array.isArray(params) ? params as Array<{ axisValue?: string; seriesIndex?: number; seriesName?: string; color?: string }> : [];
  const day = String(items[0]?.axisValue || "");
  const index = props.payload.dates.indexOf(day);
  if (index < 0) return "";
  const coverage = props.payload.coverage[index];
  const rows = items.map((item) => {
    const itemIndex = item.seriesIndex ?? displayedSeries.value.findIndex(series => series.memberName === item.seriesName);
    const series = displayedSeries.value[itemIndex];
    const values = series?.lineValues || series?.values || [];
    const current = values[index];
    const previous = index > 0 ? values[index - 1] : null;
    const delta = typeof current === "number" && typeof previous === "number" ? current - previous : null;
    if (current === null || current === undefined) return "";
    const deltaText = delta === null ? "—" : `${delta > 0 ? "+" : ""}${formatValue(delta)}`;
    return `<div class="structure-line-tooltip-row"><span><i style="background:${item.color}"></i>${escapeHtml(item.seriesName)}</span><strong>${formatValue(current)}<small>变化 ${deltaText}</small></strong></div>`;
  }).filter(Boolean).join("");
  const missing = coverage?.missingExchanges?.length ? `；缺少 ${coverage.missingExchanges.join("、")}` : "";
  return `<div class="structure-line-tooltip"><strong>${escapeHtml(day)}</strong>${rows}`
    + `<hr><span>覆盖 ${coverage?.dataQualityStatus || "未知"}${missing}</span></div>`;
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  chart.setOption({
    animationDuration: 200,
    backgroundColor: "transparent",
    aria: { enabled: true, decal: { show: false } },
    color: colors,
    tooltip: {
      trigger: "axis", axisPointer: { type: "line", lineStyle: { color: palette.textDisabled } },
      backgroundColor: palette.chartTooltip, borderColor: palette.chartTooltipBorder,
      textStyle: { color: palette.textPrimary, fontSize: 12 }, confine: true, formatter: tooltipHtml,
    },
    legend: {
      type: "scroll", data: displayedSeries.value.map(item => item.memberName), top: 0, right: 8,
      itemWidth: 14, itemHeight: 8, textStyle: { color: palette.textSecondary, fontSize: 11 },
    },
    grid: { left: 64, right: 18, top: 46, bottom: 48 },
    xAxis: {
      type: "category", data: props.payload.dates, boundaryGap: false,
      axisLine: { lineStyle: { color: palette.chartGrid } },
      axisLabel: { color: palette.chartAxis, fontSize: 11, hideOverlap: true }, axisTick: { show: false },
    },
    yAxis: {
      type: "value", name: props.axisName,
      nameTextStyle: { color: palette.chartAxis, fontSize: 11 },
      axisLabel: { color: palette.chartAxis, fontSize: 11, formatter: (value: number) => formatValue(value) },
      splitLine: { lineStyle: { color: palette.chartGrid } },
    },
    dataZoom: [
      { type: "inside", start: 0, end: 100, filterMode: "none" },
      { type: "slider", start: 0, end: 100, bottom: 4, height: 14, borderColor: palette.divider, backgroundColor: palette.surfaceElevated, fillerColor: `${palette.accent}26`, handleStyle: { color: palette.accent }, textStyle: { color: palette.chartAxis }, filterMode: "none" },
    ],
    series: displayedSeries.value.map((item, index) => ({
      id: item.memberKey, name: item.memberName, type: "line" as const,
      data: item.lineValues || item.values, showSymbol: false, connectNulls: false,
      lineStyle: { width: 1.5, color: colors[index % colors.length] },
      itemStyle: { color: colors[index % colors.length] },
      emphasis: { focus: "series" as const },
    })),
  }, true);
}

function resize(): void { chart?.resize(); }
watch(() => [props.payload, theme.palette, lineScope.value], () => void nextTick(render), { deep: true });
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
  <div class="structure-line-controls">
    <span>独立品种折线 · {{ displayedSeries.length }} 条；点击图例可隐藏/恢复品种，缺失日不连线。</span>
    <el-radio-group v-if="payload.lineSeries" v-model="lineScope" size="small" aria-label="折线品种范围">
      <el-radio-button value="all">全部已计算品种</el-radio-button>
      <el-radio-button value="area">当前面积图成员</el-radio-button>
    </el-radio-group>
  </div>
  <div ref="element" class="structure-line-chart" data-test="futures-structure-line-chart" :data-series-count="displayedSeries.length" role="img" :aria-label="ariaLabel" />
</template>

<style scoped>
.structure-line-chart { height: 780px; width: 100%; }
.structure-line-controls { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin: 16px 0 8px; color: var(--ml-text-secondary); font-size: 12px; }
</style>

<style>
.structure-line-tooltip { min-width: 225px; max-height: 340px; overflow-y: auto; }
.structure-line-tooltip-row { display: flex; justify-content: space-between; gap: 18px; line-height: 1.7; }
.structure-line-tooltip-row i { display: inline-block; width: 7px; height: 7px; margin-right: 6px; border-radius: 50%; }
.structure-line-tooltip-row strong { text-align: right; }
.structure-line-tooltip-row small { display: block; color: #94a3b8; font-size: 10px; font-weight: 400; }
.structure-line-tooltip hr { border: 0; border-top: 1px solid rgba(127, 127, 127, .35); margin: 6px 0; }
</style>
