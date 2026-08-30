<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";

export interface SeriesPoint {
  t: string;
  value: number | null;
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
    chartType?: "line" | "bar";
    opacity?: number;
  }>(),
  { title: "", unit: "", height: 280, area: true, rangeDays: 0, color: "", chartType: "line", opacity: 0.16, series: () => [] },
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

const seriesColors = ["accent", "priceUp", "priceDown", "warning", "info", "highlight"] as const;

function seriesData(item: NamedSeries): Array<number | null> {
  const byTime = new Map(item.points.map((point) => [point.t, point.value]));
  return categories.value.map((time) => {
    const value = byTime.get(time);
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  });
}

function render(): void {
  if (!element.value || !hasData.value) return;
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  const colorOf = (index: number): string =>
    palette[seriesColors[index % seriesColors.length] as keyof typeof palette];
  chart.setOption(
    {
      backgroundColor: "transparent",
      color: props.color ? [props.color] : seriesColors.map((_name, index) => colorOf(index)),
      animationDuration: 320,
      tooltip: {
        trigger: "axis",
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
        boundaryGap: false,
        axisLine: { lineStyle: { color: palette.chartGrid } },
        axisLabel: { color: palette.chartAxis, fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: palette.chartAxis, fontSize: 11 },
        splitLine: { lineStyle: { color: palette.chartGrid } },
        name: props.unit,
        nameTextStyle: { color: palette.chartAxis, fontSize: 10, align: "right" },
      },
      dataZoom: [
        { type: "inside", start: 0, end: 100 },
        {
          type: "slider",
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
        smooth: props.chartType === "line",
        showSymbol: false,
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
  () => [filteredSeries.value, theme.palette, props.rangeDays, props.color, props.chartType, props.opacity],
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
    <div ref="element" class="chart-root" />
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
