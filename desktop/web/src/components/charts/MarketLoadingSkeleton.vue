<script setup lang="ts">
withDefaults(defineProps<{ kind: "list" | "chart"; rows?: number }>(), { rows: 12 });
</script>

<template>
  <div class="market-skeleton" :class="`is-${kind}`" data-test="market-loading-skeleton" :data-kind="kind" role="status" aria-label="行情数据正在加载">
    <template v-if="kind === 'list'"><div v-for="row in rows" :key="row" class="skeleton-row"><span v-for="column in 8" :key="column" class="shimmer" /></div></template>
    <template v-else><div class="skeleton-quote"><span v-for="column in 7" :key="column" class="shimmer" /></div><div class="skeleton-chart"><span v-for="line in 4" :key="line" class="grid-line" /><i v-for="bar in 22" :key="bar" class="skeleton-candle shimmer" :style="{ left: `${5 + bar * 4}%`, top: `${26 + Math.sin(bar / 3) * 13}%`, height: `${18 + bar % 5 * 4}%` }" /></div></template>
  </div>
</template>

<style scoped>
.market-skeleton { width: 100%; min-width: 0; overflow: hidden; pointer-events: none; }
.skeleton-row { display: grid; grid-template-columns: 1.3fr .9fr repeat(6, 1fr); gap: 18px; height: 33px; align-items: center; padding: 0 14px; border-bottom: 1px solid var(--ml-divider); }
.shimmer { position: relative; display: block; overflow: hidden; border-radius: 4px; background: var(--ml-surface-selected); }
.shimmer::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--ml-surface-elevated) 80%, transparent), transparent); transform: translateX(-100%); animation: market-shimmer 1.4s ease-in-out infinite; }
.skeleton-row span { height: 10px; }.skeleton-row span:nth-child(2n) { width: 70%; }
.is-chart { height: 100%; min-height: 170px; padding: 15px; box-sizing: border-box; background: var(--ml-surface); }
.skeleton-quote { display: flex; gap: 12px; height: 35px; }.skeleton-quote span { flex: 1; height: 12px; }
.skeleton-chart { position: relative; height: calc(100% - 35px); border-left: 1px solid var(--ml-divider); border-bottom: 1px solid var(--ml-divider); }
.grid-line { position: absolute; left: 0; right: 0; top: 20%; border-top: 1px solid var(--ml-divider); }.grid-line:nth-child(2) { top: 40%; }.grid-line:nth-child(3) { top: 60%; }.grid-line:nth-child(4) { top: 80%; }
.skeleton-candle { position: absolute; width: 1.9%; min-width: 3px; }
@keyframes market-shimmer { to { transform: translateX(100%); } }
@media (prefers-reduced-motion: reduce) { .shimmer::after { animation: none; display: none; } }
</style>
