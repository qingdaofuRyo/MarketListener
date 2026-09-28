<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";
import type { PositionTrendSeries } from "../../domain/futuresMemberCharts";

const props = withDefaults(defineProps<{
  title: string;
  dates: string[];
  series: PositionTrendSeries[];
  changeTotals?: Array<number | null>;
  area?: boolean;
}>(), { area: false, changeTotals: () => [] });
const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;
const hasData = computed(() => props.series.some(item => item.values.some(value => value !== null)));
const hasChanges = computed(() => props.changeTotals.some(value => value !== null));
const colors = ["#2563eb", "#d946ef", "#16a34a", "#ea580c", "#0891b2", "#7c3aed", "#dc2626", "#65a30d"];

function formatAmount(value: number | null, signed = true): string {
  return value === null ? "未公布" : `${signed && value > 0 ? "+" : ""}${value.toLocaleString("zh-CN")}`;
}

function tooltipText(params: unknown): string {
  const items = Array.isArray(params) ? params as Array<{ axisValue?: string; seriesName?: string }> : [];
  const day = String(items[0]?.axisValue || "");
  const index = props.dates.indexOf(day);
  if (index < 0) return "";
  const visible = new Set(items.map(item => item.seriesName));
  const rows = props.series.flatMap(item => {
    const value = item.values[index];
    if (!visible.has(item.name) || value === null) return [];
    const change = item.changes[index];
    const detail = item.details?.[index];
    if (detail) {
      const sides = `多 ${formatAmount(detail.longPosition, false)} / 空 ${formatAmount(detail.shortPosition, false)}`;
      const changes = `多 ${formatAmount(detail.longChange)} / 空 ${formatAmount(detail.shortChange)}`;
      const exchanges = detail.exchanges.length ? ` · ${detail.exchanges.join("、")}` : "";
      return [`${item.name}：${value.toLocaleString("zh-CN")} 张（${sides}）· 增减（${changes}）${exchanges}`];
    }
    return [`${item.name}：${value.toLocaleString("zh-CN")} 张 · 增减 ${formatAmount(change)}`];
  });
  const total = props.changeTotals[index];
  if (hasChanges.value) rows.push(`当日已公布增减合计：${total === null ? "不可用" : `${total > 0 ? "+" : ""}${total.toLocaleString("zh-CN")}`} 张`);
  return [day, ...rows].join("\n");
}

function render(): void {
  if (!element.value) { chart?.dispose(); chart = undefined; return; }
  if (chart && chart.getDom() !== element.value) { chart.dispose(); chart = undefined; }
  chart ??= echarts.init(element.value);
  if (!hasData.value) { chart.clear(); return; }
  const palette = theme.palette;
  chart.setOption({
    backgroundColor: "transparent",
    animationDuration: 220,
    aria: { enabled: true, decal: { show: false } },
    color: colors,
    tooltip: {
      trigger: "axis", renderMode: "richText", axisPointer: { type: "line" },
      formatter: tooltipText,
      backgroundColor: palette.chartTooltip,
      borderColor: palette.chartTooltipBorder,
      textStyle: { color: palette.textPrimary, fontSize: 12 },
      confine: true,
    },
    legend: {
      type: "scroll", top: 0, right: 4,
      data: [...props.series.map(item => item.name), ...(hasChanges.value ? ["已公布增减合计"] : [])],
      textStyle: { color: palette.textSecondary, fontSize: 11 },
    },
    grid: { left: 62, right: hasChanges.value ? 62 : 16, top: 38, bottom: 48 },
    xAxis: {
      type: "category", data: props.dates, boundaryGap: false,
      axisLine: { lineStyle: { color: palette.chartGrid } },
      axisLabel: { color: palette.chartAxis, hideOverlap: true },
    },
    yAxis: [
      {
        type: "value", name: "已公布持仓（张）",
        nameTextStyle: { color: palette.chartAxis },
        axisLabel: { color: palette.chartAxis, formatter: (value: number) => value.toLocaleString("zh-CN") },
        splitLine: { lineStyle: { color: palette.chartGrid } },
      },
      ...(hasChanges.value ? [{
        type: "value", name: "增减（张）", nameTextStyle: { color: palette.chartAxis },
        axisLabel: { color: palette.chartAxis, formatter: (value: number) => value.toLocaleString("zh-CN") },
        splitLine: { show: false },
      }] : []),
    ],
    dataZoom: [
      { type: "inside", start: 0, end: 100, filterMode: "none" },
      { type: "slider", start: 0, end: 100, bottom: 4, height: 14, filterMode: "none" },
    ],
    series: [...props.series.map((item, index) => ({
      id: item.key, name: item.name, type: "line", data: item.values,
      yAxisIndex: 0,
      stack: props.area ? "published-position" : undefined,
      areaStyle: props.area ? { opacity: 0.58 } : undefined,
      showSymbol: item.values.filter(value => value !== null).length < 3,
      symbolSize: 7, connectNulls: false, smooth: false,
      sampling: props.dates.length > 1500 ? "lttb" : undefined,
      lineStyle: { width: props.area ? 1.3 : 1.8, color: colors[index % colors.length] },
      itemStyle: { color: colors[index % colors.length] },
      emphasis: { focus: "series" },
    })), ...(hasChanges.value ? [{
      id: "published-change-total", name: "已公布增减合计", type: "bar",
      data: props.changeTotals, yAxisIndex: 1, barMaxWidth: 14,
      itemStyle: {
        color: (item: { value: number | null }) => (item.value ?? 0) >= 0 ? "#dc2626" : "#16a34a",
      },
    }] : [])],
  }, true);
}

function resize(): void { chart?.resize(); }
watch(() => [props.dates, props.series, props.changeTotals, props.area, theme.palette], () => void nextTick(render), { deep: true });
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
  <article class="position-chart-card">
    <h3>{{ title }}</h3>
    <div v-if="hasData" ref="element" class="trend-chart" role="img" :aria-label="title" />
    <div v-else class="chart-empty">暂无已公布的历史持仓序列</div>
  </article>
</template>

<style scoped>
.position-chart-card { min-width: 0; padding: 12px; border: 1px solid var(--ml-divider); border-radius: 8px; background: var(--ml-surface-elevated); }
.position-chart-card h3 { margin: 0; font-size: 13px; }
.trend-chart { width: 100%; height: 320px; }
.chart-empty { display: grid; place-items: center; min-height: 280px; color: var(--ml-text-secondary); font-size: 12px; }
</style>
