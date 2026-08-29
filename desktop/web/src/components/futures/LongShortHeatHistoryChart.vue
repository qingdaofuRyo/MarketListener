<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { LongShortHeatPoint } from "../../domain/futures";
import { combineHeatScores, signedHeat } from "../../domain/futures";
import { useThemeStore } from "../../stores/theme";

const props = withDefaults(defineProps<{
  points: LongShortHeatPoint[];
  breadthWeight: number;
  fundWeight: number;
  fundUnit?: string;
  min?: number;
  max?: number;
}>(), { fundUnit: "亿元", min: -100, max: 100, points: () => [] });

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;
let updateFrame: number | undefined;
let legendSelection: Record<string, boolean> = {};

const categories = computed(() => props.points.map((point) => point.tradeDate));
const breadthData = computed(() => props.points.map((point) => point.breadthScore10));
const fundData = computed(() => props.points.map((point) => point.fundScore10));
const totalData = computed(() => props.points.map((point) =>
  combineHeatScores(point.breadthScore10, point.fundScore10, props.breadthWeight, props.fundWeight),
));
const hasData = computed(() => props.points.some((point) =>
  point.breadthScore10 !== null || point.fundScore10 !== null,
));
const lastTotal = computed(() => totalData.value.at(-1) ?? null);
const lastBreadth = computed(() => breadthData.value.at(-1) ?? null);
const lastFund = computed(() => fundData.value.at(-1) ?? null);
function seriesChecksum(values: Array<number | null>): string {
  return values.reduce<number>(
    (total, value, index) => total + (value === null ? 0 : value * (index + 1)),
    0,
  ).toFixed(6);
}
const totalChecksum = computed(() => seriesChecksum(totalData.value));
const breadthChecksum = computed(() => seriesChecksum(breadthData.value));
const fundChecksum = computed(() => seriesChecksum(fundData.value));

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char] || char));
}

function formatFund(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "暂无数据";
  if (props.fundUnit === "元") {
    return `${(value / 100_000_000).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}亿元`;
  }
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}${escapeHtml(props.fundUnit)}`;
}

function formatCoverage(point: LongShortHeatPoint): string {
  const coverage = point.coverage;
  if (typeof coverage === "number" && Number.isFinite(coverage)) {
    return `${((coverage <= 1 ? coverage : coverage / 100) * 100).toFixed(1)}%`;
  }
  if (coverage && typeof coverage === "object") {
    const variety = typeof coverage.variety === "number" ? coverage.variety : null;
    const fund = typeof coverage.fund === "number" ? coverage.fund : null;
    const percent = (value: number) => `${(value <= 1 ? value * 100 : value).toFixed(1)}%`;
    return [variety === null ? "" : `品种 ${percent(variety)}`, fund === null ? "" : `资金 ${percent(fund)}`]
      .filter(Boolean)
      .join(" · ") || "暂无数据";
  }
  return "暂无数据";
}

function tooltipHtml(params: unknown): string {
  const items = Array.isArray(params) ? params as Array<{ axisValue?: string }> : [];
  const tradeDate = String(items[0]?.axisValue || "");
  const point = props.points.find((item) => item.tradeDate === tradeDate);
  if (!point) return "";
  const palette = theme.palette;
  const dot = (color: string) => `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${color};margin-right:6px"></span>`;
  const row = (label: string, value: string, color?: string) =>
    `<div style="display:flex;justify-content:space-between;gap:22px;line-height:1.8">`
    + `<span>${color ? dot(color) : ""}${label}</span><strong>${value}</strong></div>`;
  const total = combineHeatScores(point.breadthScore10, point.fundScore10, props.breadthWeight, props.fundWeight);
  return `<div style="min-width:245px"><strong>${escapeHtml(point.tradeDate)}</strong>`
    + `<div style="margin-top:5px">${row("总多空热度", signedHeat(total), palette.heatTotal)}`
    + `${row("品种多空热度", signedHeat(point.breadthScore10), palette.heatBreadth)}`
    + `${row("资金多空热度", signedHeat(point.fundScore10), palette.heatFund)}</div>`
    + `<div style="height:1px;background:${palette.divider};margin:6px 0"></div>`
    + row("当前权重", `品种 ${Math.round(props.breadthWeight * 100)}% · 资金 ${Math.round(props.fundWeight * 100)}%`)
    + row("上涨 / 下跌 / 平盘", `${point.upVarietyCount} / ${point.downVarietyCount} / ${point.flatVarietyCount}`)
    + row("上涨沉淀资金", formatFund(point.upFund))
    + row("下跌沉淀资金", formatFund(point.downFund))
    + row("数据覆盖率", formatCoverage(point))
    + row("窗口状态", point.isWarmup ? "预热期" : "完整10交易日")
    + `</div>`;
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  if (!hasData.value) {
    chart.clear();
    return;
  }
  const palette = theme.palette;
  const useSampling = props.points.length > 1500;
  chart.setOption({
    backgroundColor: "transparent",
    animationDuration: 260,
    animationDurationUpdate: 140,
    aria: { enabled: true, decal: { show: false } },
    color: [palette.heatTotal, palette.heatBreadth, palette.heatFund],
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
      data: ["总多空热度", "品种多空热度", "资金多空热度"],
      selected: legendSelection,
      top: 0,
      right: 4,
      type: "scroll",
      icon: "roundRect",
      itemWidth: 16,
      itemHeight: 4,
      textStyle: { color: palette.textSecondary, fontSize: 11 },
    },
    grid: { left: 54, right: 18, top: 36, bottom: 48 },
    xAxis: {
      type: "category",
      data: categories.value,
      boundaryGap: false,
      axisLine: { lineStyle: { color: palette.chartGrid } },
      axisLabel: { color: palette.chartAxis, fontSize: 11, hideOverlap: true },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      min: props.min,
      max: props.max,
      interval: 50,
      axisLabel: {
        color: palette.chartAxis,
        fontSize: 11,
        formatter: (value: number) => `${value > 0 ? "+" : ""}${value}`,
      },
      splitLine: { lineStyle: { color: palette.chartGrid } },
    },
    dataZoom: [
      { type: "inside", start: 0, end: 100, filterMode: "none" },
      {
        type: "slider",
        start: 0,
        end: 100,
        bottom: 4,
        height: 14,
        borderColor: palette.divider,
        backgroundColor: palette.surfaceElevated,
        fillerColor: `${palette.accent}26`,
        handleStyle: { color: palette.accent },
        textStyle: { color: palette.chartAxis },
        filterMode: "none",
      },
    ],
    series: [
      {
        id: "total-heat",
        name: "总多空热度",
        type: "line",
        data: totalData.value,
        smooth: false,
        showSymbol: false,
        connectNulls: false,
        sampling: useSampling ? "lttb" : undefined,
        lineStyle: { width: 2.3, color: palette.heatTotal },
        itemStyle: { color: palette.heatTotal },
        emphasis: { focus: "series" },
      },
      {
        id: "breadth-heat",
        name: "品种多空热度",
        type: "line",
        data: breadthData.value,
        smooth: false,
        showSymbol: false,
        connectNulls: false,
        sampling: useSampling ? "lttb" : undefined,
        lineStyle: { width: 1.7, color: palette.heatBreadth },
        itemStyle: { color: palette.heatBreadth },
        emphasis: { focus: "series" },
      },
      {
        id: "fund-heat",
        name: "资金多空热度",
        type: "line",
        data: fundData.value,
        smooth: false,
        showSymbol: false,
        connectNulls: false,
        sampling: useSampling ? "lttb" : undefined,
        lineStyle: { width: 1.7, color: palette.heatFund },
        itemStyle: { color: palette.heatFund },
        emphasis: { focus: "series" },
      },
      {
        id: "neutral-line",
        name: "多空均衡参考线",
        type: "line",
        data: categories.value.map(() => 0),
        showSymbol: false,
        silent: true,
        tooltip: { show: false },
        lineStyle: { width: 1, type: "dashed", color: palette.textDisabled },
        emphasis: { disabled: true },
      },
    ],
  }, true);
  chart.off("legendselectchanged");
  chart.on("legendselectchanged", (event: unknown) => {
    const selected = (event as { selected?: Record<string, boolean> }).selected;
    if (selected) legendSelection = { ...selected };
  });
}

function scheduleTotalUpdate(): void {
  if (updateFrame !== undefined) cancelAnimationFrame(updateFrame);
  updateFrame = requestAnimationFrame(() => {
    updateFrame = undefined;
    chart?.setOption({ series: [{ id: "total-heat", data: totalData.value }] }, { lazyUpdate: true });
  });
}

function resize(): void {
  chart?.resize();
}

watch(
  () => [props.points, props.min, props.max, props.fundUnit, theme.palette],
  () => void nextTick(render),
  { deep: true },
);
watch(() => [props.breadthWeight, props.fundWeight], scheduleTotalUpdate);

onMounted(() => {
  void nextTick(render);
  window.addEventListener("resize", resize);
  if (element.value && typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(element.value);
  }
});

onBeforeUnmount(() => {
  if (updateFrame !== undefined) cancelAnimationFrame(updateFrame);
  window.removeEventListener("resize", resize);
  resizeObserver?.disconnect();
  chart?.dispose();
  chart = undefined;
});
</script>

<template>
  <div
    class="heat-history-chart"
    data-test="heat-history-chart"
    data-y-min="-100"
    data-y-max="100"
    data-series-count="3"
    data-neutral-line="0"
    data-legend-count="3"
    :data-point-count="props.points.length"
    :data-last-value="lastTotal ?? ''"
    :data-last-breadth-value="lastBreadth ?? ''"
    :data-last-fund-value="lastFund ?? ''"
    :data-total-checksum="totalChecksum"
    :data-breadth-checksum="breadthChecksum"
    :data-fund-checksum="fundChecksum"
  >
    <div ref="element" class="chart-root" />
    <div v-if="!hasData" class="chart-empty">暂无多空热度历史数据</div>
  </div>
</template>

<style scoped>
.heat-history-chart { position: relative; width: 100%; height: 380px; }
.chart-root { width: 100%; height: 100%; }
.chart-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--ml-text-secondary);
  background: var(--ml-surface);
  font-size: 13px;
}
@media (max-width: 700px) { .heat-history-chart { height: 320px; } }
</style>
