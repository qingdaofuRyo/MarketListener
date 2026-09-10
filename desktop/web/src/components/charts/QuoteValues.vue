<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import type { KLineBar } from './KLineChart.vue';
import { changeTone, formatPercent, formatQuote, isFiniteQuote } from '../../domain/marketQuote';
import { quoteFieldGroups } from '../../domain/quoteFieldGroups';
const props = defineProps<{bar?: KLineBar | null; latestBar?: KLineBar; totalMarketCap?: number; floatMarketCap?: number; swapColors?: boolean; inline?: boolean}>();
const root = ref<HTMLElement>();
const groupWidths = ref<number[]>([]);
let observer: ResizeObserver | undefined;
onMounted(() => {
  observer = new ResizeObserver(() => {
    const widths = Array.from(root.value?.children || [], el => el.getBoundingClientRect().width);
    if (widths.some((width,i)=>width!==groupWidths.value[i])) groupWidths.value = widths;
  });
  if (root.value) observer.observe(root.value);
});
onBeforeUnmount(() => observer?.disconnect());
function number(value: unknown, unit = false, percent = false): string {
  return isFiniteQuote(value) ? (percent ? formatPercent(value) : formatQuote(value, 2, unit)) : '--';
}
const activeBar = computed(() => props.bar || props.latestBar);
const direction = computed(() => changeTone(activeBar.value?.pctChange ?? activeBar.value?.change));
const fields = computed(() => {
  const b = activeBar.value;
  return Object.fromEntries([
    ['开', number(b?.open)], ['收', number(b?.close)],
    ['高', number(b?.high)], ['低', number(b?.low)],
    ['结', number(b?.settlement)], ['振幅', number(b?.amplitude, false, true)],
    ['涨幅', number(b?.pctChange, false, true)], ['涨跌', number(b?.change)],
    ['量', number(b?.volume, true)], ['额', number(b?.amount, true)],
    ['持仓量', number(b?.openInterest, true)], ['沉淀资金', number(props.latestBar?.capitalDeposit, true)],
    ['总市值', number(props.totalMarketCap, true)], ['流通市值', number(props.floatMarketCap, true)],
  ]);
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
function numericSize(name: string, group: number): string {
  const width = (groupWidths.value[group] || 132) * (group >= 5 ? 0.58 : 0.7) - 2;
  return `${Math.min(11, width / Math.max(1, fields.value[name].length) / 0.68)}px`;
}
function labelSize(name: string, group: number): string {
  return `${Math.min(11, (groupWidths.value[group] || 132) * (group >= 5 ? 0.42 : 0.3) / name.length / 1.05)}px`;
}
</script>
<template><div ref="root" class="quote-values" :class="{inline}"><div v-for="(group,index) in quoteFieldGroups" :key="group[0]" class="quote-field-group"><p v-for="name in group" :key="name" :data-field="name" :title="`${name} ${fields[name]}`"><span :style="{fontSize:labelSize(name,index)}">{{ name }}</span><strong :style="{color:color(name),fontSize:numericSize(name,index)}">{{ fields[name] }}</strong></p></div></div></template>
<style scoped>
.quote-values{display:grid;grid-template-columns:repeat(5,minmax(0,1fr)) repeat(2,minmax(0,1.4fr));gap:var(--ml-quote-grid-gap);width:100%;font-size:clamp(7px,1.65cqw,11px);font-variant-numeric:tabular-nums}
.quote-field-group{display:grid;grid-template-rows:repeat(2,18px)}
.quote-values p{display:grid;grid-template-columns:30% minmax(0,1fr);align-items:center;gap:2px;margin:0;white-space:nowrap}
.quote-field-group:nth-last-child(-n+2) p{grid-template-columns:42% minmax(0,1fr)}
.quote-values span{color:var(--ml-text-secondary)}.quote-values strong{font-family:Consolas,monospace;font-weight:600;white-space:nowrap}
</style>
