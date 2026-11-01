<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";

export interface SeriesPoint {
  t: string;
  value: number | null;
  detail?: string;
}

export interface NamedSeries {
  name: string;
  points: SeriesPoint[];
}

const props = withDefaults(
  defineProps<{
    title?: string;
    series: NamedSeries[];
    unit?: string;
    height?: number;
    area?: boolean;
    rangeDays?: number;
    color?: string;
    colors?: string[];
    initialWindowPoints?: number;
    smooth?: boolean;
    showSymbols?: boolean;
    zeroBaseline?: boolean;
    chartType?: "line" | "bar";
    opacity?: number;
  }>(),
  { title: "", unit: "", height: 280, area: true, rangeDays: 0, color: "", colors: () => [], initialWindowPoints: 0, smooth: true, showSymbols: false, zeroBaseline: false, chartType: "line", opacity: 0.16, series: () => [] },
);

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;

const filteredSeries = computed(() => {
  if (!props.rangeDays || props.rangeDays <= 0) return props.series;
  const cutoff = Date.now() - props.rangeDays * 86_400_000;
  return props.series.map((item) => ({
    name: item.name,
    points: item.points.filter((point) => new Date(point.t).getTime() >= cutoff),
  }));
});

const categories = computed(() => {
  const values = new Set<string>();
  for (const item of filteredSeries.value) {
    for (const point of item.points) values.add(point.t);
  }
  return [...values].sort();
});

const hasData = computed(() =>
  filteredSeries.value.some((item) => item.points.some((point) => typeof point.value === "number" && Number.isFinite(point.value))),
);
const accessibleSummary = computed(() => {
  const details = filteredSeries.value.map(item => {
    const latest = [...item.points].reverse().find(point => typeof point.value === "number" && Number.isFinite(point.value));
    const gaps = item.points.some(point => point.value === null) ? "，缺报处断线" : "";
    return latest ? `${item.name}最近观测 ${latest.t} ${latest.value}${props.unit}${gaps}` : `${item.name}暂无观测`;
  });
  return `${props.title || "历史走势"}：${details.join("；") || "暂无可用数据"}`;
});

const seriesColors = ["accent", "priceUp", "priceDown", "warning", "info", "highlight"] as const;

function seriesData(item: NamedSeries): Array<number | null> {
  const byTime = new Map(item.points.map((point) => [point.t, point.value]));
  return categories.value.map((time) => {
    const value = byTime.get(time);
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  });
}

interface AxisTooltipItem { axisValue?: string; name?: string; seriesIndex?: number }

function escapeTooltip(value: string): string {
  return value.replace(/[&<>"']/gu, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character] || character);
}

function tooltipHtml(raw: unknown): string {
  const items = (Array.isArray(raw) ? raw : [raw]) as AxisTooltipItem[];
  const day = String(items[0]?.axisValue ?? items[0]?.name ?? "");
  const rows = filteredSeries.value.flatMap(series => {
    const point = series.points.find(candidate => candidate.t === day);
    if (!point) return [];
    if (typeof point.value !== "number" || !Number.isFinite(point.value)) {
      return point.detail ? [`<div>${escapeTooltip(series.name)}：暂无可信值`
        + `<br/><small>${escapeTooltip(point.detail)}</small></div>`] : [];
    }
    const value = point.value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
    return [`<div>${escapeTooltip(series.name)}：<strong>${value}${escapeTooltip(props.unit)}</strong>`
      + (point.detail ? `<br/><small>${escapeTooltip(point.detail)}</small>` : "") + "</div>"];
  });
  return `<div>${escapeTooltip(day)}</div>${rows.join("")}`;
}

function render(): void {
  if (!element.value || !hasData.value) return;
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  const colorOf = (index: number): string =>
    palette[seriesColors[index % seriesColors.length] as keyof typeof palette];
  const initialStart = props.initialWindowPoints > 0 && categories.value.length > props.initialWindowPoints
    ? 100 * (1 - props.initialWindowPoints / categories.value.length) : 0;
  chart.setOption(
    {
      backgroundColor: "transparent",
      color: props.colors.length ? props.colors : props.color ? [props.color] : seriesColors.map((_name, index) => colorOf(index)),
      animationDuration: 320,
      tooltip: {
        trigger: "axis",
        formatter: tooltipHtml,
        backgroundColor: palette.chartTooltip,
        borderColor: palette.chartTooltipBorder,
        textStyle: { color: palette.textPrimary, fontSize: 12 },
        confine: true,
      },
      legend: {
        top: 0,
        right: 4,
        type: "scroll",
        icon: "roundRect",
        itemWidth: 14,
        itemHeight: 4,
        textStyle: { color: palette.textSecondary, fontSize: 11 },
      },
      grid: { left: 58, right: 18, top: 34, bottom: 30 },
      xAxis: {
        type: "category",
        data: categories.value,
        boundaryGap: props.chartType === "bar",
        axisLine: { lineStyle: { color: palette.chartGrid } },
        axisLabel: { color: palette.chartAxis, fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        scale: !props.zeroBaseline,
        axisLabel: { color: palette.chartAxis, fontSize: 11 },
        splitLine: { lineStyle: { color: palette.chartGrid } },
        name: props.unit,
        nameTextStyle: { color: palette.chartAxis, fontSize: 10, align: "right" },
      },
      dataZoom: [
        { type: "inside", start: initialStart, end: 100 },
        {
          type: "slider",
          start: initialStart,
          end: 100,
          bottom: 0,
          height: 14,
          borderColor: palette.divider,
          backgroundColor: palette.surfaceElevated,
          fillerColor: `${palette.accent}26`,
          handleStyle: { color: palette.accent },
          textStyle: { color: palette.chartAxis },
        },
      ],
      series: filteredSeries.value.map((item, index) => ({
        name: item.name,
        type: props.chartType,
        data: seriesData(item),
        smooth: props.chartType === "line" && props.smooth,
        showSymbol: props.chartType === "line" && props.showSymbols,
        connectNulls: false,
        lineStyle: { width: 1.6 },
        areaStyle: props.area && props.chartType === "line" && index === 0 ? { opacity: props.opacity } : undefined,
        emphasis: { focus: "series" },
      })),
    },
    true,
  );
}

function resize(): void {
  chart?.resize();
}

watch(
  () => [filteredSeries.value, theme.palette, props.rangeDays, props.color, props.colors, props.initialWindowPoints, props.smooth, props.showSymbols, props.zeroBaseline, props.chartType, props.area, props.opacity],
  () => void nextTick(render),
  { deep: true },
);

onMounted(() => {
  void nextTick(render);
  window.addEventListener("resize", resize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", resize);
  chart?.dispose();
  chart = undefined;
});
</script>

<template>
  <div class="series-chart chart-box" :style="{ height: `${height}px` }">
    <div v-if="title" class="chart-title">{{ title }}</div>
    <div ref="element" class="chart-root" role="img" :aria-label="accessibleSummary" />
    <div v-if="!hasData" class="chart-empty">暂无该指标数据</div>
  </div>
</template>

<style scoped>
.series-chart { position: relative; width: 100%; }
.chart-title {
  position: absolute;
  top: 4px;
  left: 4px;
  z-index: 2;
  font-size: 12px;
  font-weight: 600;
  color: var(--ml-text-secondary);
  pointer-events: none;
}
.chart-root { width: 100%; height: 100%; }
.chart-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--ml-text-secondary);
  font-size: 13px;
  background: var(--ml-surface);
}
</style>
