<script setup lang="ts">
defineProps<{ kind: "heat" | "overview" | "bubble" | "ranking" | "timeline" }>();
</script>

<template>
  <div class="futures-skeleton" :class="`is-${kind}`" data-test="futures-loading-skeleton" :data-kind="kind" role="status" aria-label="图表数据正在加载">
    <div class="sk-heading shimmer" />
    <div v-if="kind === 'heat'" class="sk-heat">
      <div class="sk-gauge shimmer" />
      <div class="sk-facts"><span v-for="index in 4" :key="index" class="shimmer" /></div>
    </div>
    <template v-else-if="kind === 'timeline'"><div class="sk-track shimmer" /><div class="sk-ticks"><span v-for="index in 5" :key="index" class="shimmer" /></div></template>
    <template v-else>
      <div v-if="kind === 'bubble'" class="sk-inputs"><span class="shimmer" /><span class="shimmer" /></div>
      <div class="sk-plot" :class="`plot-${kind}`">
        <span v-for="index in 5" :key="index" class="sk-gridline" />
        <template v-if="kind === 'bubble'"><i v-for="index in 7" :key="index" class="sk-dot shimmer" :class="`dot-${index}`" /></template>
        <template v-else><i v-for="index in 8" :key="index" class="sk-bar shimmer" :class="`bar-${index}`" /></template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.futures-skeleton { min-width: 0; padding: 10px 4px; overflow: hidden; }
.sk-heading { height: 15px; width: 35%; border-radius: 6px; margin: 4px 0 18px; }
.shimmer { position: relative; overflow: hidden; background: var(--ml-surface-selected); }
.shimmer::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--ml-surface-elevated) 75%, transparent), transparent); transform: translateX(-100%); animation: skeleton-shimmer 1.4s ease-in-out infinite; }
.sk-heat { display: flex; align-items: center; gap: 26px; min-height: 285px; }
.sk-gauge { width: 210px; height: 210px; flex: none; border-radius: 50%; }
.sk-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; flex: 1; }
.sk-facts span { height: 86px; border-radius: 12px; }
.sk-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
.sk-inputs span { height: 35px; border-radius: 8px; }
.sk-plot { position: relative; min-height: 325px; border-left: 1px solid var(--ml-divider); border-bottom: 1px solid var(--ml-divider); }
.plot-bubble { width: 100%; max-width: 1120px; height: clamp(680px, 78vh, 860px); margin-inline: auto; }
.plot-overview { min-height: 360px; }.plot-ranking { min-height: 420px; }
.sk-gridline { position: absolute; left: 0; right: 0; top: 20%; height: 1px; background: var(--ml-divider); opacity: .65; }
.sk-gridline:nth-child(2) { top: 40%; }.sk-gridline:nth-child(3) { top: 60%; }.sk-gridline:nth-child(4) { top: 80%; }.sk-gridline:nth-child(5) { top: 100%; }
.sk-dot { position: absolute; width: 25px; height: 25px; border-radius: 50%; left: 22%; top: 65%; }
.dot-2 { width: 47px; height: 47px; left: 45%; top: 46%; }.dot-3 { left: 70%; top: 30%; }.dot-4 { width: 32px; height: 32px; left: 32%; top: 27%; }.dot-5 { left: 59%; top: 71%; }.dot-6 { width: 38px; height: 38px; left: 80%; top: 62%; }.dot-7 { left: 13%; top: 41%; }
.sk-bar { position: absolute; left: 17%; top: 11%; width: 64%; height: 13px; border-radius: 8px; }
.bar-2 { top: 21%; width: 44%; }.bar-3 { top: 31%; width: 72%; }.bar-4 { top: 41%; width: 38%; }.bar-5 { top: 51%; width: 57%; }.bar-6 { top: 61%; width: 48%; }.bar-7 { top: 71%; width: 68%; }.bar-8 { top: 81%; width: 31%; }
.sk-track { height: 12px; border-radius: 99px; margin: 16px 4px; }.sk-ticks { display: flex; justify-content: space-between; }.sk-ticks span { width: 65px; height: 10px; border-radius: 5px; }
@keyframes skeleton-shimmer { to { transform: translateX(100%); } }
@media (max-width: 600px) { .sk-heat { flex-direction: column; align-items: stretch; }.sk-gauge { align-self: center; }.sk-ticks span:nth-child(2n) { display: none; } }
@media (max-width: 900px) { .plot-bubble { height: clamp(440px, 68vh, 620px); } }
@media (prefers-reduced-motion: reduce) { .shimmer::after { animation: none; display: none; } }
</style>
