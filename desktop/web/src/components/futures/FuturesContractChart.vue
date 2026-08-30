<script setup lang="ts">
import * as echarts from "echarts";
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { FuturesContractSeriesResponse } from "../../domain/futures";
import { useThemeStore } from "../../stores/theme";

const props = defineProps<{ payload: FuturesContractSeriesResponse }>();

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;

const seriesColors = {
  openInterest: "#FF9800",
  notional: "#9C27B0",
  basis: "#00BCD4",
};

function value(value: number | null | undefined, digits = 0): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function reason(metric: string): string {
  return props.payload.availability[metric]?.reason || "—";
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  const dates = props.payload.points.map((item) => item.tradingDay);
  const hasPrice = props.payload.availability.price?.available;
  const hasOpenInterest = props.payload.availability.openInterest?.available;
  const hasNotional = props.payload.availability.notional?.available;
  const hasBasis = props.payload.availability.basis?.available;
  chart.setOption({
    animationDuration: 180,
    animationDurationUpdate: 100,
    backgroundColor: "transparent",
    aria: { enabled: true, decal: { show: false } },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", lineStyle: { color: palette.textDisabled } },
      backgroundColor: palette.chartTooltip,
      borderColor: palette.chartTooltipBorder,
      textStyle: { color: palette.textPrimary, fontSize: 12 },
      confine: true,
      formatter(items: unknown): string {
        const rows = Array.isArray(items) ? items as Array<{ axisValue?: string; seriesName?: string; value?: unknown; marker?: string }> : [];
        const point = props.payload.points.find((item) => item.tradingDay === String(rows[0]?.axisValue || ""));
        if (!point) return "";
        const ohlc = point.open === null || point.close === null || point.low === null || point.high === null
          ? "—"
          : `开 ${value(point.open, 2)} / 高 ${value(point.high, 2)} / 低 ${value(point.low, 2)} / 收 ${value(point.close, 2)}`;
        return [
          `<strong>${point.tradingDay}</strong>`,
          `<div>价格/K线：${ohlc}</div>`,
          `<div>持仓量：${value(point.openInterest)} 张</div>`,
          `<div>名义持仓规模：${value(point.notionalRmb)}${point.notionalRmb === null ? `（${reason("notional")}）` : " 元"}</div>`,
          `<div>基差：${value(point.basisRmb)}${point.basisRmb === null ? `（${reason("basis")}）` : " 元"}</div>`,
        ].join("");
      },
    },
    legend: {
      data: ["价格/K线", "持仓量", "名义持仓规模", "基差"],
      selected: {
        "价格/K线": hasPrice,
        "持仓量": hasOpenInterest,
        "名义持仓规模": hasNotional,
        "基差": hasBasis,
      },
      top: 0,
      right: 8,
      itemWidth: 14,
      itemHeight: 8,
      textStyle: { color: palette.textSecondary, fontSize: 11 },
    },
    grid: { left: 64, right: 148, top: 46, bottom: 48 },
    xAxis: {
      type: "category",
      data: dates,
      scale: true,
      boundaryGap: true,
      axisLine: { lineStyle: { color: palette.chartGrid } },
      axisLabel: { color: palette.chartAxis, fontSize: 11, hideOverlap: true },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: "value", name: "价格（来源单位）", scale: true, position: "left",
        axisLabel: { color: palette.chartAxis, fontSize: 11 }, splitLine: { lineStyle: { color: palette.chartGrid } },
      },
      {
        type: "value", name: "持仓（张）", position: "right", show: hasOpenInterest,
        axisLabel: { color: seriesColors.openInterest, fontSize: 11, formatter: (item: number) => value(item) }, splitLine: { show: false },
      },
      {
        type: "value", name: "名义规模（元）", position: "right", offset: 66, show: hasNotional,
        axisLabel: { color: seriesColors.notional, fontSize: 11, formatter: (item: number) => value(item) }, splitLine: { show: false },
      },
      {
        type: "value", name: "基差（元）", position: "left", offset: 48, show: hasBasis,
        axisLabel: { color: seriesColors.basis, fontSize: 11, formatter: (item: number) => value(item) }, splitLine: { show: false },
      },
    ],
    dataZoom: [
      { type: "inside", start: 0, end: 100, filterMode: "none" },
      { type: "slider", start: 0, end: 100, bottom: 4, height: 14, borderColor: palette.divider, backgroundColor: palette.surfaceElevated, fillerColor: `${palette.accent}26`, handleStyle: { color: palette.accent }, textStyle: { color: palette.chartAxis }, filterMode: "none" },
    ],
    series: [
      {
        id: "price", name: "价格/K线", type: "candlestick", yAxisIndex: 0,
        data: props.payload.points.map((item) => item.open === null || item.close === null || item.low === null || item.high === null
          ? "-" : [item.open, item.close, item.low, item.high]),
        itemStyle: { color: "#F23645", color0: "#089981", borderColor: "#F23645", borderColor0: "#089981" },
      },
      {
        id: "open-interest", name: "持仓量", type: "line", yAxisIndex: 1,
        data: props.payload.points.map((item) => item.openInterest), showSymbol: false, connectNulls: false,
        lineStyle: { color: seriesColors.openInterest, width: 1.5 }, itemStyle: { color: seriesColors.openInterest },
      },
      {
        id: "notional", name: "名义持仓规模", type: "line", yAxisIndex: 2,
        data: props.payload.points.map((item) => item.notionalRmb), showSymbol: false, connectNulls: false,
        lineStyle: { color: seriesColors.notional, width: 1.5 }, itemStyle: { color: seriesColors.notional },
      },
      {
        id: "basis", name: "基差", type: "line", yAxisIndex: 3,
        data: props.payload.points.map((item) => item.basisRmb), showSymbol: false, connectNulls: false,
        lineStyle: { color: seriesColors.basis, width: 1.5 }, itemStyle: { color: seriesColors.basis },
      },
    ],
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
  <div ref="element" class="futures-contract-chart" data-test="futures-contract-chart" role="img" aria-label="商品合约价格、持仓量、名义持仓规模和基差多轴图" />
</template>

<style scoped>
.futures-contract-chart { height: 440px; width: 100%; }
</style>
