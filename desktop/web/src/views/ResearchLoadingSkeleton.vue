<script setup lang="ts">
withDefaults(defineProps<{ kind?: "cards" | "chart" | "table"; rows?: number }>(), {
  kind: "chart",
  rows: 5,
});
</script>

<template>
  <div class="research-skeleton" :class="`skeleton-${kind}`" role="status" aria-label="正在读取数据" aria-busy="true">
    <template v-if="kind === 'cards'">
      <div v-for="row in rows" :key="row" class="skeleton-card">
        <span class="skeleton-line title" /><span class="skeleton-line" /><span class="skeleton-line short" />
        <div class="skeleton-tags"><span /><span /></div>
      </div>
    </template>
    <template v-else-if="kind === 'table'">
      <div v-for="row in rows" :key="row" class="skeleton-row"><span /><span /><span /><span /></div>
    </template>
    <template v-else>
      <div class="skeleton-chart-heading"><span class="skeleton-line title" /><span class="skeleton-line short" /></div>
      <div class="skeleton-chart-grid"><div class="skeleton-chart-wave" /></div>
      <div class="skeleton-chart-axis"><span /><span /><span /><span /></div>
    </template>
  </div>
</template>

<style scoped>
.research-skeleton { overflow: hidden; color: var(--ml-text-disabled); }
.skeleton-line, .skeleton-tags span, .skeleton-row span, .skeleton-chart-axis span { display: block; height: 10px; border-radius: 4px; background: var(--ml-surface-selected); position: relative; overflow: hidden; }
.skeleton-line::after, .skeleton-row span::after, .skeleton-tags span::after, .skeleton-chart-wave::after { content: ""; position: absolute; inset: 0; background: linear-gradient(100deg, transparent 20%, color-mix(in srgb, var(--ml-text-disabled) 14%, transparent) 50%, transparent 80%); transform: translateX(-100%); animation: research-shimmer 1.8s ease-in-out infinite; }
.skeleton-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
.skeleton-card { display: grid; gap: 15px; padding: 20px; min-height: 150px; border: 1px solid var(--ml-divider); border-radius: 10px; }
.skeleton-line.title { width: 46%; height: 15px; }
.skeleton-line.short { width: 65%; }
.skeleton-tags { display: flex; gap: 8px; }
.skeleton-tags span { width: 54px; height: 22px; }
.skeleton-table { display: grid; padding: 8px 0; }
.skeleton-row { display: grid; grid-template-columns: 1.8fr 1.2fr 1fr 1fr; gap: 28px; padding: 19px 16px; border-bottom: 1px solid var(--ml-divider); }
.skeleton-chart { min-height: 235px; padding: 18px 12px; }
.skeleton-chart-heading { display: flex; justify-content: space-between; gap: 50%; margin-bottom: 20px; }
.skeleton-chart-grid { position: relative; height: 170px; margin: 0 10px 0 32px; border-left: 1px solid var(--ml-divider); border-bottom: 1px solid var(--ml-divider); background: repeating-linear-gradient(to bottom, transparent 0, transparent 41px, var(--ml-divider) 42px, transparent 43px); overflow: hidden; }
.skeleton-chart-wave { position: absolute; inset: 25px 0 0; opacity: .4; background: var(--ml-surface-selected); clip-path: polygon(0 77%, 10% 57%, 20% 63%, 32% 39%, 44% 51%, 58% 25%, 72% 39%, 84% 10%, 100% 24%, 100% 100%, 0 100%); }
.skeleton-chart-axis { display: flex; justify-content: space-between; gap: 15px; margin: 14px 10px 0 32px; }
.skeleton-chart-axis span { width: 52px; height: 7px; }
@keyframes research-shimmer { 100% { transform: translateX(100%); } }
@media (prefers-reduced-motion: reduce) { .research-skeleton *::after { animation: none; } }
</style>
