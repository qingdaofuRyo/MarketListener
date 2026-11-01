<script setup lang="ts">
import { computed } from "vue";
import type { FuturesStructureResponse } from "../../domain/futures";

const props = defineProps<{ payload: FuturesStructureResponse }>();
const latest = computed(() => props.payload.coverage.at(-1));
const lines = computed(() => props.payload.lineSeries ?? props.payload.series.filter(item => item.memberKey !== "OTHER"));
const latestCount = computed(() => lines.value.filter(item => typeof (item.lineValues ?? item.values).at(-1) === "number").length);
const completeDays = computed(() => props.payload.totals.filter(value => typeof value === "number").length);
const exchangeNames: Record<string, string> = { SHFE: "上期所", INE: "能源中心", DCE: "大商所", CZCE: "郑商所", GFEX: "广期所", CFFEX: "中金所" };
const missingExchanges = computed(() => latest.value?.missingExchanges?.map(key => exchangeNames[key] ?? key).join("、"));
</script>

<template>
  <div class="coverage-summary" data-test="structure-coverage-summary">
    <span>面积图：固定前 {{ payload.rankLimit ?? 40 }} 名＋其他；折线图默认逐一显示全部已计算品种，不受前40名限制。</span>
    <span>最新交易日 {{ latest?.tradeDate ?? '未知' }}：可绘制 {{ latestCount }} 个品种；所选范围有 {{ completeDays }}/{{ payload.dates.length }} 个完整市场日。已计算品种不等于全部上市期货品种。</span>
    <span v-if="missingExchanges">缺少官网行情：{{ missingExchanges }}；这些交易所的品种尚未计入，不能将现有数据解释为全市场。</span>
    <span v-if="latest?.missingProducts?.length">字段缺失/未核验品种：{{ latest.missingProducts.join('、') }}。缺结算价 {{ latest.missingSettlementCount ?? 0 }} 条、持仓量 {{ latest.missingOpenInterestCount ?? 0 }} 条、乘数 {{ latest.missingMultiplierCount ?? 0 }} 条、成交量 {{ latest.missingVolumeCount ?? 0 }} 条。</span>
  </div>
</template>

<style scoped>
.coverage-summary { display: grid; gap: 5px; margin-bottom: 12px; padding: 10px 12px; border-left: 3px solid var(--ml-warning, #d97706); background: var(--ml-surface-elevated); color: var(--ml-text-secondary); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
</style>
