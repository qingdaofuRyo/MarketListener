<script setup lang="ts">
import { computed } from 'vue';
import type { KLineBar } from './KLineChart.vue';
const props = defineProps<{bar?: KLineBar | null; latestBar?: KLineBar; totalMarketCap?: number; floatMarketCap?: number; swapColors?: boolean; inline?: boolean}>();
function number(value: unknown, unit = false, percent = false): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const scale = unit && Math.abs(value) >= 1e8 ? 1e8 : unit && Math.abs(value) >= 1e4 ? 1e4 : 1;
  return (value / scale).toLocaleString('zh-CN', {maximumFractionDigits: 2}) + (scale === 1e8 ? '亿' : scale === 1e4 ? '万' : '') + (percent ? '%' : '');
}
const direction = computed(() => (props.bar?.change ?? ((props.bar?.close ?? 0) - (props.bar?.open ?? 0))) >= 0);
const pairs = computed(() => {
  const b = props.bar;
  return [
    [['开盘价', number(b?.open)], ['收盘价', number(b?.close)]],
    [['最高价', number(b?.high)], ['最低价', number(b?.low)]],
    [['结算价', number(b?.settlement)], ['振幅', number(b?.amplitude, false, true)]],
    [['涨幅', number(b?.pctChange, false, true)], ['涨跌', number(b?.change)]],
    [['成交量', number(b?.volume, true)], ['成交额', number(b?.amount, true)]],
    [['持仓量', number(b?.openInterest, true)], ['沉淀资金', number(props.latestBar?.capitalDeposit, true)]],
    [['总市值', number(props.totalMarketCap, true)], ['流通市值', number(props.floatMarketCap, true)]],
  ];
});
function color(name: string): string {
  if (['开盘价','收盘价','成交量','成交额','涨幅','涨跌'].includes(name)) return direction.value !== Boolean(props.swapColors) ? '#e53935' : '#16a36a';
  if (['最低价','持仓量','流通市值'].includes(name)) return '#9867d5';
  return name === '振幅' ? '#cf8615' : '#3689d8';
}
</script>
<template><div class="quote-values" :class="{inline}"><div v-for="(pair, index) in pairs" :key="index" class="quote-pair"><p v-for="[name, value] in pair" :key="name" :data-field="name"><span>{{ name }}</span><strong :style="{color:color(name)}">{{ value }}</strong></p></div></div></template>
<style scoped>.quote-values{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:5px;width:100%;font-size:11px}.quote-pair{min-width:0}.quote-pair p{display:flex;gap:5px;margin:2px 0;white-space:nowrap}.quote-pair span{color:var(--ml-text-secondary)}.quote-pair strong{font-weight:500}.inline{display:flex;flex-wrap:wrap;column-gap:14px;row-gap:0;width:auto}.inline .quote-pair{display:contents}.inline p{margin:0;line-height:22px}</style>
