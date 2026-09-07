<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { apiGet } from "../domain/api";
import { useTargetMarketStore } from "../stores/targetMarket";

interface Category { id: string; label: string; }
const router = useRouter();
const target = useTargetMarketStore();
const categories = ref<Category[]>([]);
onMounted(async () => {
  target.start();
  try { categories.value = (await apiGet<{ items: Category[] }>("/api/market/categories", undefined, { persist: true })).items; } catch { categories.value = []; }
});
function open(instrumentId: string): void { void router.push({ path: `/market/instrument/${encodeURIComponent(instrumentId)}/` }); }
function price(value?: number): string { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 4 }) : "—"; }
</script>

<template>
  <main class="target-market-page">
    <header><h1>目标行情</h1><p>“已开仓”仅表示信号监控状态，不代表真实成交。</p></header>
    <section class="target-filter-panel">
      <div><span>市场</span><nav aria-label="目标行情市场筛选"><button :class="{active: !target.selectedMarkets.length}" @click="target.toggleMarket('all')">全部市场</button><button v-for="item in categories" :key="item.id" :class="{active: target.selectedMarkets.includes(item.id)}" @click="target.toggleMarket(item.id)">{{ item.label }}</button></nav></div>
      <div><span>策略</span><nav aria-label="目标行情策略筛选"><button :class="{active: !target.selectedStrategies.length}" @click="target.toggleStrategy('all')">全部策略</button><button v-for="item in target.strategies" :key="item.strategyId" :class="{active: target.selectedStrategies.includes(item.strategyId)}" @click="target.toggleStrategy(item.strategyId)">{{ item.displayName }}</button></nav></div>
      <div class="actions"><el-button type="primary" :disabled="target.loading || !target.strategies.length" @click="target.scan()">检查开仓信号</el-button><el-button :disabled="target.loading || !target.monitored.length" @click="target.scan(true)">更新已开仓监控</el-button><el-button v-if="target.loading" @click="target.stopScan">停止扫描</el-button><small>{{ target.progress }}</small></div>
    </section>
    <el-alert v-if="target.error" type="warning" :title="target.error" :closable="false" />
    <section v-loading="target.loading" class="target-results" aria-label="目标行情结果"><button v-for="item in target.filtered" :key="`${item.instrumentId}-${item.direction}`" @click="open(item.instrumentId)"><b>{{ item.symbol || item.instrumentId }}</b><span>{{ item.name || '—' }}</span><strong>{{ price(item.latestPrice ?? item.lastClose) }}</strong><em>{{ item.direction === 'long' ? '多头' : '空头' }}</em></button><p v-if="!target.filtered.length && !target.loading">暂无同时满足当前筛选的标的。</p></section>
    <details class="target-monitor-panel" open><summary>已开仓信号监控 · {{ target.monitored.length }}</summary><div v-for="item in target.monitored" :key="`${item.instrumentId}-${item.direction}`"><button @click="open(item.instrumentId)">{{ item.name || item.instrumentId }}</button><span>{{ item.direction === 'long' ? '多头' : '空头' }}</span><span>最近：{{ item.latestSignal?.action }} · {{ item.latestSignal?.strategyName }}</span></div><details><summary>最近信号记录</summary><div v-for="(event, index) in target.events" :key="index"><span>{{ event.instrumentId }}</span><b>{{ event.action }}</b><span>{{ event.strategyName }}</span></div></details></details>
  </main>
</template>

<style scoped>
.target-market-page{max-width:1440px;margin:0 auto;padding:20px 24px}.target-market-page h1{margin:0;font-size:20px}.target-market-page header p{color:var(--ml-text-secondary);font-size:12px}.target-filter-panel{display:grid;gap:10px;padding:12px;border:1px solid var(--ml-divider);background:var(--ml-surface)}.target-filter-panel>div{display:flex;gap:10px;align-items:flex-start}.target-filter-panel>div>span{flex:0 0 3em;color:var(--ml-text-secondary);font-size:12px;padding-top:6px}.target-filter-panel nav{display:flex;flex-wrap:wrap;gap:4px}.target-filter-panel button{border:1px solid var(--ml-divider);border-radius:4px;background:transparent;color:var(--ml-text-secondary);padding:4px 8px;cursor:pointer}.target-filter-panel button.active{border-color:var(--ml-accent);color:var(--ml-text-primary);background:var(--ml-accent-soft)}.actions{align-items:center!important}.actions small{color:var(--ml-text-secondary)}.target-results{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;margin-top:14px}.target-results>button{display:grid;grid-template-columns:1fr auto;gap:3px 8px;padding:10px;border:1px solid var(--ml-divider);background:var(--ml-surface);color:var(--ml-text-primary);text-align:left;cursor:pointer}.target-results span,.target-results em{font-size:11px;color:var(--ml-text-secondary)}.target-results strong,.target-results em{text-align:right;font-style:normal}.target-results p{color:var(--ml-text-secondary)}
.target-monitor-panel{display:grid;gap:7px;margin-top:16px;padding-top:12px;border-top:1px solid var(--ml-divider);font-size:12px}.target-monitor-panel>div,.target-monitor-panel details>div{display:flex;gap:10px}.target-monitor-panel button{border:0;background:transparent;color:var(--ml-accent);cursor:pointer;padding:0}
</style>
