<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { apiGet, formatTime } from "../domain/api";
import { useTargetMarketStore } from "../stores/targetMarket";

interface Category { id: string; label: string; }
const router = useRouter();
const target = useTargetMarketStore();
const categories = ref<Category[]>([]);
onMounted(async () => {
  target.start();
  try { categories.value = (await apiGet<{ items: Category[] }>("/api/market/categories", undefined, { persist: true })).items; } catch { categories.value = []; }
});
const actionNames:Record<string,string> = {watch:"关注",unwatch:"取消关注",open:"开仓",add:"加仓",reduce:"减仓",close:"平仓"};
function percent(value?:number|null):string {return typeof value==="number" && Number.isFinite(value) ? (value*100).toFixed(2)+"%" : "—";}
function open(instrumentId: string): void { void router.push({ path: `/market/instrument/${encodeURIComponent(instrumentId)}/` }); }
function price(value?: number | null): string { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 4 }) : "—"; }
</script>

<template>
  <main class="target-market-page">
    <header><h1>目标行情</h1><p>展示关注、仓位、择时三部分齐全的组合策略。配置比例和操作信号用于观察，不代表真实成交。</p></header>
    <section class="target-filter-panel">
      <div><span>市场</span><nav aria-label="目标行情市场筛选"><button :class="{active: !target.selectedMarkets.length}" @click="target.toggleMarket('all')">全部市场</button><button v-for="item in categories" :key="item.id" :class="{active: target.selectedMarkets.includes(item.id)}" @click="target.toggleMarket(item.id)">{{ item.label }}</button></nav></div>
      <div><span>组合</span><nav aria-label="目标行情策略筛选"><button :class="{active: !target.selectedStrategies.length}" @click="target.toggleStrategy('all')">全部策略</button><button v-for="item in target.strategies" :key="item.strategyId" :class="{active: target.selectedStrategies.includes(item.strategyId)}" @click="target.toggleStrategy(item.strategyId)">{{ item.displayName }}</button></nav></div>
      <div class="actions"><el-button type="primary" :disabled="target.loading || !target.strategies.length" @click="target.scan()">扫描组合策略</el-button><el-button :disabled="target.loading || !target.monitored.length" @click="target.scan(true)">更新已关注标的</el-button><el-button v-if="target.loading" @click="target.stopScan">停止扫描</el-button><small>{{ target.progress }}</small></div>
    </section>
    <el-alert v-if="target.error" type="warning" :title="target.error" :closable="false" />
    <section v-loading="target.loading" class="composite-results" aria-label="目标行情结果">
      <article v-for="item in target.filtered" :key="`${item.strategyId}-${item.instrumentId}`">
        <h2>{{item.strategyName}} <small>{{item.direction==='long'?'多头关注':'空头关注'}}</small></h2>
        <button class="instrument" @click="open(item.instrumentId)">{{item.name || item.symbol || item.instrumentId}}</button>
        <p>{{item.symbol}} · 最新 {{price(item.latestPrice ?? item.lastClose)}} · {{item.positionOpen?'已开仓观察':'等待开仓时机'}}</p>
        <p>比较区间：{{formatTime(item.referenceAt)}} 至 {{formatTime(item.asOf)}}</p>
        <p>区间涨跌幅 {{price(item.changePct)}}% · 同期同向 {{item.peerCount}} 个标的</p>
        <dl><dt>盈亏比</dt><dd>{{price(item.position?.rewardRisk)}}</dd><dt>观察胜率</dt><dd>{{percent(item.position?.winRate)}}（{{item.position?.observedRounds ?? 0}}个已结束轮次）</dd><dt>建议配置比例</dt><dd>{{percent(item.position?.allocation)}}</dd><dt>资金使用率</dt><dd>{{percent(item.position?.capitalUsage)}}</dd><dt>杠杆率</dt><dd>{{price(item.position?.leverage)}} 倍</dd></dl>
        <p>最近信号：{{actionNames[item.latestSignal?.action || ''] || '暂无'}} · {{formatTime(item.latestSignal?.at)}}</p>
        <details v-if="item.peers?.length"><summary>同期同向标的（同时段表现，不代表带动关系）</summary><button v-for="peer in item.peers" :key="peer.instrumentId" @click="open(peer.instrumentId)">{{peer.instrumentId}} {{price(peer.changePct)}}%</button></details>
      </article>
      <p v-if="!target.filtered.length && !target.loading">暂无已关注的组合策略目标。请先在策略页创建并启用组合策略。</p>
    </section>
    <details class="target-monitor-panel"><summary>最近组合策略信号</summary><div v-for="(event,index) in target.events" :key="index"><span>{{event.instrumentId}}</span><b>{{actionNames[event.action] || event.action}}</b><span>{{event.strategyName}}</span><span>{{formatTime(event.at)}}</span></div></details>
  </main>
</template>

<style scoped>
.target-market-page{max-width:1440px;margin:0 auto;padding:20px 24px}.target-market-page h1{margin:0;font-size:20px}.target-market-page header p{color:var(--ml-text-secondary);font-size:12px}.target-filter-panel{display:grid;gap:10px;padding:12px;border:1px solid var(--ml-divider);background:var(--ml-surface)}.target-filter-panel>div{display:flex;gap:10px;align-items:flex-start}.target-filter-panel>div>span{flex:0 0 3em;color:var(--ml-text-secondary);font-size:12px;padding-top:6px}.target-filter-panel nav{display:flex;flex-wrap:wrap;gap:4px}.target-filter-panel button{border:1px solid var(--ml-divider);border-radius:4px;background:transparent;color:var(--ml-text-secondary);padding:4px 8px;cursor:pointer}.target-filter-panel button.active{border-color:var(--ml-accent);color:var(--ml-text-primary);background:var(--ml-accent-soft)}.actions{align-items:center!important}.actions small{color:var(--ml-text-secondary)}.target-results{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;margin-top:14px}.target-results>button{display:grid;grid-template-columns:1fr auto;gap:3px 8px;padding:10px;border:1px solid var(--ml-divider);background:var(--ml-surface);color:var(--ml-text-primary);text-align:left;cursor:pointer}.target-results span,.target-results em{font-size:11px;color:var(--ml-text-secondary)}.target-results strong,.target-results em{text-align:right;font-style:normal}.target-results p{color:var(--ml-text-secondary)}
.target-monitor-panel{display:grid;gap:7px;margin-top:16px;padding-top:12px;border-top:1px solid var(--ml-divider);font-size:12px}.target-monitor-panel>div,.target-monitor-panel details>div{display:flex;gap:10px}.target-monitor-panel button{border:0;background:transparent;color:var(--ml-accent);cursor:pointer;padding:0}
.composite-results{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px;margin-top:16px}.composite-results article{border:1px solid var(--ml-divider);background:var(--ml-surface);padding:16px;min-width:0}.composite-results h2{font-size:16px;margin:0 0 12px}.composite-results h2 small{font-size:12px;color:var(--ml-accent)}.composite-results p,.composite-results dl{font-size:12px}.composite-results dl{display:grid;grid-template-columns:7em 1fr;gap:6px}.composite-results dd{margin:0;font-variant-numeric:tabular-nums}.composite-results button{background:transparent;color:var(--ml-accent);border:0;cursor:pointer}.composite-results details button{display:block;margin-top:8px}.composite-results .instrument{font-size:16px;font-weight:600}
</style>
