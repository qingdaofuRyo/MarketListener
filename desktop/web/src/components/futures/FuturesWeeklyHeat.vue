<script setup lang="ts">
import { computed } from "vue";
import type { FuturesWeeklyHeatResponse } from "../../domain/futures";
import { heatStateLabel } from "../../domain/futures";
import LongShortHeatGauge from "./LongShortHeatGauge.vue";
import FuturesTimelineChart from "./FuturesTimelineChart.vue";
const props = defineProps<{ payload: FuturesWeeklyHeatResponse }>();
const newest = computed(() => props.payload.latest || props.payload.points.at(-1));
const latest = computed(() => newest.value?.totalScore != null
  ? newest.value
  : [...props.payload.points].reverse().find(point => point.totalScore != null) || newest.value);
const showingEarlierDay = computed(() => Boolean(latest.value && newest.value && latest.value.tradeDate !== newest.value.tradeDate));
const pendingDays = computed(() => latest.value
  ? props.payload.points.filter(point => point.tradeDate > latest.value!.tradeDate && point.totalScore == null).length
  : 0);
const venueNames: Record<string, string> = { CFFEX: "中金所", SHFE: "上期所", INE: "上期能源", DCE: "大商所", CZCE: "郑商所", GFEX: "广期所" };
const missingVenues = computed(() => {
  const codes = newest.value?.coverage?.dailyMissingExchanges || newest.value?.coverage?.missingExchanges || [];
  return codes.map(code => venueNames[code] || code).join("、");
});
const completeDay = computed(() => Boolean(latest.value?.totalScore != null && !latest.value.coverage?.dailyMissingExchanges?.length));
const completeStreak = computed(() => Boolean(latest.value?.totalScore != null && !latest.value.pendingWeek));
const dates = computed(() => props.payload.points.map(point => point.tradeDate));
const scoreFields = [
  { key: "totalScore", title: "总体多空热度" },
  { key: "breadthScore", title: "数量热度" },
  { key: "streakScore", title: "连续周热度" },
  { key: "magnitudeScore", title: "涨跌幅热度" },
] as const;
const scoreSeries = computed(() => scoreFields.map(field => ({
  key: field.key, name: field.title,
  values: props.payload.points.map(point => point[field.key]),
})));

function count(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString("zh-CN");
}

function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

</script>
<template>
  <section class="panel weekly-heat" data-test="futures-weekly-heat">
    <header><h2>多空热度</h2><div class="heat-controls"><slot name="controls" /></div></header>
    <p v-if="showingEarlierDay" class="heat-date-status" role="status">
      显示 {{ latest?.tradeDate }} 最近可计算热度；截至 {{ newest?.tradeDate }}，{{ pendingDays }} 个交易日总分待补{{ missingVenues ? `（缺${missingVenues}官网日行情）` : '' }}。
    </p>
    <p v-else-if="newest?.totalScore == null" class="heat-date-status" role="status">
      当前范围尚无可计算总热度{{ missingVenues ? `；缺${missingVenues}官网日行情` : '' }}。
    </p>
    <div class="summary-cards">
      <LongShortHeatGauge
        title="总体多空热度"
        :value="latest?.totalScore ?? null"
        :status="heatStateLabel(latest?.totalScore, payload.config.stateBands)"
        :bands="payload.config.stateBands"
        :min="payload.config.score.min"
        :max="payload.config.score.max"
      />
      <article class="fact-card" data-test="heat-fact" aria-label="上涨与下跌合约数量">
        <h3>合约数量</h3>
        <div class="fact-row long"><span>上涨</span><strong>{{ count(completeDay ? latest?.upCount : null) }}</strong><small>个合约</small></div>
        <div class="fact-row short"><span>下跌</span><strong>{{ count(completeDay ? latest?.downCount : null) }}</strong><small>个合约</small></div>
        <p>平盘 {{ count(completeDay ? latest?.flatCount : null) }} 个合约</p>
      </article>
      <article class="fact-card" data-test="heat-fact" aria-label="连续上涨与连续下跌合约数量">
        <h3>连续周</h3>
        <div class="fact-row long"><span>连涨 ≥2 周</span><strong>{{ count(completeStreak ? latest?.streakUpCount : null) }}</strong><small>个合约</small></div>
        <div class="fact-row short"><span>连跌 ≥2 周</span><strong>{{ count(completeStreak ? latest?.streakDownCount : null) }}</strong><small>个合约</small></div>
      </article>
      <article class="fact-card" data-test="heat-fact" aria-label="上涨与下跌加权结算变化">
        <h3>加权涨跌幅</h3>
        <div class="fact-row long"><span>上涨</span><strong>{{ percent(completeDay ? latest?.upWeightedReturnPct : null) }}</strong></div>
        <div class="fact-row short"><span>下跌</span><strong>{{ percent(completeDay ? latest?.downWeightedReturnPct : null) }}</strong></div>
      </article>
    </div>
    <FuturesTimelineChart title="每日多空热度" :dates="dates" :series="scoreSeries" unit="热度" />
  </section>
</template>
<style scoped>
.weekly-heat { min-width: 0; }
.heat-date-status { margin: -3px 0 14px; padding: 8px 11px; border: 1px solid var(--ml-divider); border-radius: 7px; background: var(--ml-surface-elevated); color: var(--ml-text-secondary); font-size: 12px; line-height: 1.5; }
header { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; min-height: 36px; margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--ml-divider); }
h2 { margin: 0; font-size: 16px; font-weight: 650; }
.heat-controls { min-width: 0; max-width: 100%; }
.summary-cards { display: grid; grid-template-columns: minmax(0,1.2fr) repeat(3, minmax(0, 1fr)); gap: 12px; align-items: stretch; margin-bottom: 18px; }
.summary-cards > * { min-width: 0; }
.summary-cards :deep(.gauge-card), .fact-card {
  height: 238px; border: 1px solid var(--ml-divider); border-radius: 10px;
  background: var(--ml-surface-elevated); box-shadow: none;
}
.summary-cards :deep(.gauge-card) { background: color-mix(in srgb, var(--ml-accent) 3%, var(--ml-surface-elevated)); }
.summary-cards :deep(.gauge-card h3) { font-size: 13px; color: var(--ml-text-secondary); }
.summary-cards :deep(.gauge-card .gauge-reading strong) { font-size: 34px; }
.fact-card { min-width: 0; display: flex; flex-direction: column; padding: 17px 16px; }
.fact-card h3 { margin: 0 0 20px; color: var(--ml-text-primary); font-size: 13px; font-weight: 600; }
.fact-row { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: baseline; gap: 5px; margin-bottom: 17px; white-space: nowrap; }
.fact-row span { grid-column: 1 / -1; }
.fact-row span { min-width: 0; color: var(--ml-text-secondary); font-size: 12px; }
.fact-row strong { min-width: 0; font: 650 clamp(15px, 1.35vw, 21px)/1.2 ui-monospace, Consolas, monospace; letter-spacing: -.025em; }
.fact-row small { flex: none; color: var(--ml-text-secondary); font-size: 11px; }
.fact-row.long strong { color: var(--ml-price-up); }
.fact-row.short strong { color: var(--ml-price-down); }
.fact-card p { margin: auto 0 0; color: var(--ml-text-secondary); font-size: 12px; }
.fact-card, .summary-cards :deep(.gauge-card) { transition: border-color 180ms; }
.fact-card:hover, .summary-cards :deep(.gauge-card:hover) { border-color: color-mix(in srgb, var(--ml-accent) 32%, var(--ml-divider)); }
@media (max-width: 1180px) { .summary-cards { grid-template-columns: repeat(2, minmax(0,1fr)); } .summary-cards :deep(.gauge-card) { grid-column: 1 / -1; height: 224px; } .fact-card { height: 216px; } .fact-row strong { font-size: 21px; } }
@media (max-width: 500px) { .summary-cards { gap: 8px; }.fact-card { height: 218px; padding: 14px 10px; }.fact-card h3 { font-size: 12px; }.fact-row strong { font-size: 16px; }.fact-row span { font-size: 11px; }.fact-row small { font-size: 10px; }.heat-controls { width: 100%; }.heat-controls :deep(.el-select) { width: 100%; } }
@media (prefers-reduced-motion: reduce) { .fact-card, .summary-cards :deep(.gauge-card) { transition: none; } }
</style>
