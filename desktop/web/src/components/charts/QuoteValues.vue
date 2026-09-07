<script setup lang="ts">
import { computed } from 'vue';
import type { KLineBar } from './KLineChart.vue';
const props = defineProps<{bar?: KLineBar | null; latestBar?: KLineBar; totalMarketCap?: number; floatMarketCap?: number; swapColors?: boolean; inline?: boolean}>();
function number(value: unknown, unit = false, percent = false): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const scale = unit && Math.abs(value) >= 1e8 ? 1e8 : unit && Math.abs(value) >= 1e4 ? 1e4 : 1;
  return (value / scale).toLocaleString('zh-CN', {maximumFractionDigits: 2}) + (scale === 1e8 ? '亿' : scale === 1e4 ? '万' : '') + (percent ? '%' : '');
}
const activeBar = computed(() => props.bar || props.latestBar);
const direction = computed(() => (activeBar.value?.change ?? ((activeBar.value?.close ?? 0) - (activeBar.value?.open ?? 0))) >= 0);
const fields = computed(() => {
  const b = activeBar.value;
  return [
    ['开', number(b?.open)], ['收', number(b?.close)],
    ['高', number(b?.high)], ['低', number(b?.low)],
    ['结', number(b?.settlement)], ['振幅', number(b?.amplitude, false, true)],
    ['涨幅', number(b?.pctChange, false, true)], ['涨跌', number(b?.change)],
    ['量', number(b?.volume, true)], ['额', number(b?.amount, true)],
    ['持仓量', number(b?.openInterest, true)], ['沉淀资金', number(props.latestBar?.capitalDeposit, true)],
    ['总市值', number(props.totalMarketCap, true)], ['流通市值', number(props.floatMarketCap, true)],
  ];
});
function color(name: string): string {
  if (['开','收','量','额','涨幅','涨跌'].includes(name)) return direction.value !== Boolean(props.swapColors) ? 'var(--ml-price-up)' : 'var(--ml-price-down)';
  if (['低','持仓量','流通市值'].includes(name)) return 'var(--ml-chart-secondary)';
  return name === '振幅' ? 'var(--ml-highlight)' : 'var(--ml-info)';
}
</script>
<template><div class="quote-values" :class="{inline}"><p v-for="[name, value] in fields" :key="name" :data-field="name" :title="`${name} ${value}`"><span>{{ name }}</span><strong :style="{color:color(name)}">{{ value }}</strong></p></div></template>
<style scoped>
.quote-values{display:grid;grid-template-columns:repeat(14,minmax(0,65px));grid-auto-rows:18px;gap:2px 0;width:max-content;min-width:100%;font-size:11px;font-variant-numeric:tabular-nums}
.quote-values p{display:grid;grid-template-columns:minmax(0,2.45em) minmax(0,1fr);align-items:center;gap:2px;margin:0;min-width:0;white-space:nowrap}
.quote-values span{color:var(--ml-text-secondary);overflow:hidden;text-overflow:ellipsis}.quote-values strong{overflow:hidden;font-weight:600;text-overflow:ellipsis;white-space:nowrap}
.inline{grid-template-columns:repeat(14,minmax(0,65px));min-width:max-content}
@container (max-width: 980px){.quote-values{grid-template-columns:repeat(7,minmax(0,65px));grid-auto-rows:17px}.inline{grid-template-columns:repeat(7,minmax(0,65px))}}
</style>
