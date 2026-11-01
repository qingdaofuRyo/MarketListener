<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { futuresAxisPointer, futuresLegend, futuresTooltip, futuresZoom } from "../../domain/futuresChartDesign";
import { useThemeStore } from "../../stores/theme";

// Money inputs are in 亿元; the endpoint return is in %, while daily
// weighted changes are added arithmetically in 百分点.
const props = defineProps<{
  dates: string[];
  title: string;
  turnoverValues: Array<number | null>;
  returnValues: Array<number | null>;
  depositValues: Array<number | null>;
  returnCoverageLabels?: string[];
  directionValues?: Array<number | null>;
  directionCoverageLabels?: string[];
}>();

const colors = { turnover: "#4f70d6", returns: "#a75ac9", deposit: "#d89232", direction: "#cc5670" };
const theme = useThemeStore();
const element = ref<HTMLElement>();
const turnover = computed(() => normalize(props.turnoverValues));
const returns = computed(() => normalize(props.returnValues));
const deposit = computed(() => normalize(props.depositValues));
const direction = computed(() => normalize(props.directionValues));
const hasDirection = computed(() => direction.value.some(value => value !== null));
const hasData = computed(() => [turnover.value, returns.value, deposit.value, direction.value].some(values => values.some(value => value !== null)));
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;

function normalize(values?: Array<number | null>): Array<number | null> {
  return props.dates.map((_, index) => {
    const value = values?.[index];
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  });
}

function format(value: number | null | undefined, unit: string): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const maximumFractionDigits = (unit === "%" || unit === "百分点")
    ? value !== 0 && Math.abs(value) < .01 ? 8 : 3 : 2;
  return `${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits })} ${unit}`;
}

function escape(value: string): string {
  return value.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char] || char);
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  if (!hasData.value) { chart.clear(); return; }
  const palette = theme.palette;
  chart.setOption({
    animation: false,
    aria: { enabled: true },
    backgroundColor: "transparent",
    tooltip: {
      ...futuresTooltip(palette), trigger: "axis", axisPointer: futuresAxisPointer(palette),
      formatter(raw: unknown): string {
        const items = Array.isArray(raw) ? raw as Array<{ axisValue?: string; dataIndex?: number }> : [];
        const index = typeof items[0]?.dataIndex === "number" ? items[0].dataIndex
          : props.dates.indexOf(String(items[0]?.axisValue || ""));
        if (index < 0 || index >= props.dates.length) return "";
        const coverage = props.returnCoverageLabels?.[index];
        const directionCoverage = props.directionCoverageLabels?.[index];
        return [
          `<strong>${escape(props.dates[index])}</strong>`,
          `<div style="color:${colors.turnover}">● 当日成交额：${format(turnover.value[index], "亿元")}</div>`,
          `<div style="color:${colors.returns}">● 近1～10交易日涨跌幅：${format(returns.value[index], "%")}</div>`,
          `<div style="color:${colors.deposit}">● 沉淀资金：${format(deposit.value[index], "亿元")}</div>`,
          ...(coverage ? [`<div style="color:${palette.textSecondary};font-size:11px">${escape(coverage)}</div>`] : []),
          `<div style="color:${colors.direction}">● 近1～60交易日净涨跌幅累计：${format(direction.value[index], "百分点")}</div>`,
          ...(directionCoverage ? [`<div style="color:${palette.textSecondary};font-size:11px">${escape(directionCoverage)}</div>`] : []),
        ].join("");
      },
    },
    legend: { ...futuresLegend(palette), top: 4 },
    graphic: [{ type: "text", left: 66, top: "58%", silent: true,
      style: { text: "近1～60交易日净涨跌幅累计", fill: palette.textSecondary, fontSize: 11 } }],
    grid: [{ left: 66, right: 58, top: 58, height: "39%" },
      { left: 66, right: 58, top: "66%", bottom: 62 }],
    axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
    xAxis: [
      { type: "category", gridIndex: 0, data: props.dates, boundaryGap: false,
        axisLabel: { show: false }, axisTick: { show: false },
        axisLine: { onZero: false, lineStyle: { color: palette.chartGrid } } },
      { type: "category", gridIndex: 1, data: props.dates, boundaryGap: false,
        axisLabel: { color: palette.chartAxis, hideOverlap: true },
        axisLine: { onZero: false, lineStyle: { color: palette.chartGrid } } },
    ],
    yAxis: [
      { type: "value", name: "亿元", scale: true, nameTextStyle: { color: palette.chartAxis },
        axisLabel: { color: palette.chartAxis }, splitLine: { lineStyle: { color: palette.chartGrid } } },
      { type: "value", gridIndex: 0, name: "%", scale: true, position: "right", nameTextStyle: { color: colors.returns },
        axisLabel: { color: colors.returns }, splitLine: { show: false } },
      { type: "value", gridIndex: 1, name: "百分点", scale: false,
        nameTextStyle: { color: colors.direction }, axisLabel: { color: colors.direction },
        splitLine: { lineStyle: { color: palette.chartGrid } } },
    ],
    dataZoom: futuresZoom(palette).map(item => ({ ...item, xAxisIndex: [0, 1] })),
    series: [
      { id: "turnover", name: "当日成交额", type: "line", yAxisIndex: 0, data: turnover.value,
        showSymbol: false, symbol: "circle", symbolSize: 6, connectNulls: false,
        lineStyle: { color: colors.turnover, width: 2.3 }, itemStyle: { color: colors.turnover },
        emphasis: { focus: "series", lineStyle: { width: 3.3 } } },
      { id: "return", name: "近1～10交易日涨跌幅", type: "line", yAxisIndex: 1, data: returns.value,
        showSymbol: false, symbol: "circle", symbolSize: 6, connectNulls: false,
        lineStyle: { color: colors.returns, width: 2.3 }, itemStyle: { color: colors.returns },
        emphasis: { focus: "series", lineStyle: { width: 3.3 } } },
      { id: "deposit", name: "沉淀资金", type: "line", yAxisIndex: 0, data: deposit.value,
        showSymbol: false, symbol: "circle", symbolSize: 6, connectNulls: false,
        lineStyle: { color: colors.deposit, width: 2.3 }, itemStyle: { color: colors.deposit },
        emphasis: { focus: "series", lineStyle: { width: 3.3 } } },
      { id: "direction", name: "近1～60交易日净涨跌幅累计", type: "line", xAxisIndex: 1,
        yAxisIndex: 2, data: direction.value, showSymbol: false, symbol: "circle", symbolSize: 6,
        connectNulls: false, lineStyle: { color: colors.direction, width: 2.3 },
        itemStyle: { color: colors.direction },
        markLine: { silent: true, symbol: "none", label: { show: false },
          lineStyle: { color: palette.chartAxis, type: "dashed", opacity: .6 }, data: [{ yAxis: 0 }] },
        emphasis: { focus: "series", lineStyle: { width: 3.3 } } },
    ],
  }, true);
}

watch(() => [props.dates, props.turnoverValues, props.returnValues, props.depositValues,
  props.returnCoverageLabels, props.directionValues, props.directionCoverageLabels, theme.palette],
  () => void nextTick(render), { deep: true });
onMounted(() => {
  void nextTick(render);
  if (typeof ResizeObserver !== "undefined" && element.value) {
    observer = new ResizeObserver(() => chart?.resize());
    observer.observe(element.value);
  }
});
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose(); });
</script>

<template>
  <article class="triple-metric-card" data-test="futures-triple-metric-card">
    <h3>{{ title }}</h3>
    <div ref="element" class="triple-metric-chart" data-test="futures-triple-metric-chart"
      :data-series-count="4" :data-date-count="dates.length" :data-direction-present="hasDirection"
      role="img" :aria-label="`${title}：成交额、近1～10交易日涨跌幅、沉淀资金及近1～60交易日净涨跌幅累计`" />
    <div v-if="hasData && !hasDirection" class="direction-empty" data-test="futures-direction-history-empty">方向累计暂无可核验数据</div>
    <div v-if="!hasData" class="empty">所选范围暂无可用历史数据</div>
  </article>
</template>

<style scoped>
.triple-metric-card { position: relative; display: flex; flex-direction: column; width: 100%; height: 100%; min-height: 440px; min-width: 0; margin: 0; padding: 14px 12px 8px; border: 1px solid var(--ml-divider); border-radius: 10px; background: var(--ml-surface-elevated); box-sizing: border-box; }
h3 { flex: none; font-size: 13px; font-weight: 600; margin: 0 0 10px; padding: 0 2px 12px; border-bottom: 1px solid var(--ml-divider); }
.triple-metric-chart { flex: 1 1 auto; min-height: 420px; width: 100%; }
.empty { position: absolute; inset: 50px 0 0; display: grid; place-items: center; pointer-events: none; color: var(--ml-text-secondary); }
.direction-empty { position: absolute; left: 0; right: 0; bottom: 22%; text-align: center; pointer-events: none; color: var(--ml-text-secondary); font-size: 12px; }
@media (prefers-reduced-motion: reduce) { .triple-metric-card { transition: none; } }
</style>
