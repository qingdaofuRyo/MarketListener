<script setup lang="ts">
import { computed } from 'vue';
import type { KLineBar } from './KLineChart.vue';
import { changeTone, formatPercent, formatQuote, isFiniteQuote } from '../../domain/marketQuote';
const props = defineProps<{bar?: KLineBar | null; latestBar?: KLineBar; totalMarketCap?: number; floatMarketCap?: number; swapColors?: boolean; inline?: boolean}>();
function number(value: unknown, unit = false, percent = false): string {
  return percent ? formatPercent(value) : formatQuote(value, 2, unit);
}
const activeBar = computed(() => props.bar || props.latestBar);
const direction = computed(() => changeTone(activeBar.value?.pctChange ?? activeBar.value?.change));
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
  const keys: Record<string, keyof KLineBar> = {开:'open',收:'close',量:'volume',额:'amount',涨幅:'pctChange',涨跌:'change'};
  if (keys[name]) {
    if (!isFiniteQuote(activeBar.value?.[keys[name]])) return 'var(--ml-text-disabled)';
    const tone = direction.value;
    if (tone === 'flat' || tone === 'missing') return 'var(--ml-text-primary)';
    return (tone === 'up') !== Boolean(props.swapColors) ? 'var(--ml-price-up)' : 'var(--ml-price-down)';
  }
  if (['低','持仓量','流通市值'].includes(name)) return 'var(--ml-chart-secondary)';
  return name === '振幅' ? 'var(--ml-highlight)' : 'var(--ml-info)';
}
</script>
<template><div class="quote-values" :class="{inline}"><p v-for="[name, value] in fields" :key="name" :data-field="name" :title="`${name} ${value}`"><span>{{ name }}</span><strong :style="{color:color(name)}">{{ value }}</strong></p></div></template>
<style scoped>
.quote-values{display:grid;grid-template-columns:repeat(14,var(--ml-quote-field-width));grid-auto-rows:18px;gap:var(--ml-quote-grid-gap);width:max-content;font-size:11px;font-variant-numeric:tabular-nums}
.quote-values p{display:grid;grid-template-columns:var(--ml-quote-label-width) minmax(0,1fr);align-items:center;gap:2px;margin:0;min-width:0;white-space:nowrap}
.quote-values span{color:var(--ml-text-secondary);overflow:hidden;text-overflow:ellipsis}.quote-values strong{overflow:hidden;font-weight:600;text-overflow:ellipsis;white-space:nowrap}
@container (width < 1456px){.quote-values{grid-template-columns:repeat(7,var(--ml-quote-field-width))}}
</style>
