<script setup lang="ts">
import { computed } from "vue";
import type { KLineBar } from "./KLineChart.vue";
import MetricGrid from "../terminal/MetricGrid.vue";
import { changeTone, formatPercent, formatQuote, isFiniteQuote } from "../../domain/marketQuote";
import { quoteFieldGroups } from "../../domain/quoteFieldGroups";

const props = defineProps<{
  bar?: KLineBar | null;
  latestBar?: KLineBar;
  totalMarketCap?: number;
  floatMarketCap?: number;
  swapColors?: boolean;
  inline?: boolean;
}>();

function number(value: unknown, unit = false, percent = false): string {
  return isFiniteQuote(value) ? (percent ? formatPercent(value) : formatQuote(value, 2, unit)) : "--";
}

const activeBar = computed(() => props.bar || props.latestBar);
const direction = computed(() => changeTone(activeBar.value?.pctChange ?? activeBar.value?.change));

const fields = computed<Record<string, string>>(() => {
  const bar = activeBar.value;
  return Object.fromEntries([
    ["开", number(bar?.open)],
    ["收", number(bar?.close)],
    ["高", number(bar?.high)],
    ["低", number(bar?.low)],
    ["结", number(bar?.settlement)],
    ["振幅", number(bar?.amplitude, false, true)],
    ["涨幅", number(bar?.pctChange, false, true)],
    ["涨跌", number(bar?.change)],
    ["量", number(bar?.volume, true)],
    ["额", number(bar?.amount, true)],
    ["持仓量", number(bar?.openInterest, true)],
    ["沉淀资金", number(props.latestBar?.capitalDeposit, true)],
    ["总市值", number(props.totalMarketCap, true)],
    ["流通市值", number(props.floatMarketCap, true)],
  ]);
});

function color(name: string): string {
  const keys: Record<string, keyof KLineBar> = {
    开: "open",
    收: "close",
    量: "volume",
    额: "amount",
    涨幅: "pctChange",
    涨跌: "change",
  };
  if (keys[name]) {
    if (!isFiniteQuote(activeBar.value?.[keys[name]])) return "var(--ml-text-disabled)";
    const tone = direction.value;
    if (tone === "flat" || tone === "missing") return "var(--ml-text-primary)";
    return (tone === "up") !== Boolean(props.swapColors) ? "var(--ml-price-up)" : "var(--ml-price-down)";
  }
  if (["低", "持仓量", "流通市值"].includes(name)) return "var(--ml-chart-secondary)";
  return name === "振幅" ? "var(--ml-highlight)" : "var(--ml-info)";
}

const colors = computed<Record<string, string>>(() =>
  Object.fromEntries(quoteFieldGroups.flatMap((group) => group.map((name) => [name, color(name)]))),
);
</script>

<template>
  <MetricGrid
    :groups="quoteFieldGroups"
    :values="fields"
    :colors="colors"
    :inline="inline"
  />
</template>
