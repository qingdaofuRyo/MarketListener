<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { apiGet, apiPost } from "../domain/api";

type Method = { id: string; version: number; name: string; kind: string; category: string; summary: string; formula: string | null; requiredData: string[]; assumptions: string[]; limitations: string[]; blockReasons: string[]; implementationStatus: string; origin?: string };
type Directory = { methods: Method[]; categories: string[]; status?: string };
type Readiness = { ready: boolean; reasons: string[]; barCount?: number; firstDay?: string; lastDay?: string };
type Run = { runId: string; name: string; purpose: string; metrics: Record<string, number>; observations: { day: string; value: number }[]; limitations: string[]; barCount: number; firstDay: string; lastDay: string };
const directory = ref<Directory>({ methods: [], categories: [] });
const selected = ref<Method>();
const query = ref("");
const category = ref("all");
const kind = ref("all");
const instrumentId = ref("");
const start = ref("");
const end = ref("");
const windowDays = ref<number | undefined>();
const gate = ref<Readiness>();
const run = ref<Run>();
const busy = ref(false);
const error = ref("");
const chartElement = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;
let requestRevision = 0;
const visible = computed(() => directory.value.methods.filter((item) =>
  (category.value === "all" || item.category === category.value) &&
  (kind.value === "all" || item.kind === kind.value) &&
  `${item.name} ${item.summary}`.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())));
const metricLabels: Record<string, string> = { priceChange: "区间价格变化", annualizedVolatility: "年化历史波动率", maximumPriceDrawdown: "最大价格回撤", positiveDays: "上涨交易日", negativeDays: "下跌交易日", latestAnnualizedVolatility: "最新滚动波动率", window: "窗口交易日" };
function percentMetric(key: string): boolean { return ["priceChange", "annualizedVolatility", "maximumPriceDrawdown", "latestAnnualizedVolatility"].includes(key); }
function displayMetric(key: string, value: number): string { return percentMetric(key) ? `${(value * 100).toFixed(2)}%` : value.toLocaleString("zh-CN"); }
function select(item: Method): void { requestRevision++; selected.value = item; gate.value = undefined; run.value = undefined; error.value = ""; }
watch([instrumentId, start, end, windowDays], () => { requestRevision++; gate.value = undefined; run.value = undefined; error.value = ""; });
async function load(): Promise<void> {
  busy.value = true;
  try {
    directory.value = await apiGet<Directory>("/api/quant/methods", undefined, { force: true });
    selected.value ||= directory.value.methods[0];
  } catch (failure) { error.value = failure instanceof Error ? failure.message : "方法目录读取失败"; }
  finally { busy.value = false; }
}
async function check(): Promise<void> {
  if (!selected.value) return;
  if (!instrumentId.value || !start.value || !end.value) { error.value = "请填写标的及起止日期"; return; }
  const revision = ++requestRevision;
  busy.value = true; error.value = ""; gate.value = undefined; run.value = undefined;
  try {
    const result = await apiGet<Readiness>(`/api/quant/methods/${encodeURIComponent(selected.value.id)}/readiness`,
      { instrumentId: instrumentId.value, start: start.value, end: end.value, window: windowDays.value }, { force: true });
    if (revision === requestRevision) gate.value = result;
  } catch (failure) { if (revision === requestRevision) error.value = failure instanceof Error ? failure.message : "数据检查失败"; }
  finally { busy.value = false; }
}
async function execute(): Promise<void> {
  if (!selected.value || !gate.value?.ready) return;
  const revision = ++requestRevision;
  busy.value = true; error.value = "";
  try {
    const result = await apiPost<Run>("/api/quant/runs", { methodId: selected.value.id, instrumentId: instrumentId.value, start: start.value, end: end.value, window: windowDays.value });
    if (revision !== requestRevision) return;
    run.value = result;
    await nextTick(); drawChart();
  } catch (failure) { if (revision === requestRevision) error.value = failure instanceof Error ? failure.message : "本地计算失败"; }
  finally { busy.value = false; }
}
function drawChart(): void {
  if (!chartElement.value || !run.value) return;
  chart ??= echarts.init(chartElement.value);
  chart.setOption({ tooltip: { trigger: "axis" }, grid: { left: 55, right: 18, top: 22, bottom: 44 },
    xAxis: { type: "category", data: run.value.observations.map((item) => item.day), axisLabel: { color: "#81939e" } },
    yAxis: { type: "value", axisLabel: { color: "#81939e", formatter: (value: number) => `${(value * 100).toFixed(0)}%` } },
    series: [{ type: "line", name: run.value.name, data: run.value.observations.map((item) => item.value),
      showSymbol: false, sampling: "lttb", areaStyle: { opacity: .08 }, lineStyle: { width: 2, color: "#50c7a4" } }],
  }, true);
}
watch(chartElement, (element) => {
  observer?.disconnect(); chart?.dispose(); chart = undefined;
  if (element) { observer?.observe(element); drawChart(); }
}, { flush: "post" });
onMounted(() => { observer = new ResizeObserver(() => chart?.resize()); void load(); });
onBeforeUnmount(() => { requestRevision++; observer?.disconnect(); chart?.dispose(); });
</script>

<template>
  <section class="quant-page">
    <header class="heading"><span class="eyebrow">QUANTITATIVE RESEARCH</span><h1>金工</h1><p>阅读工具、方法与策略；只有数据和规则完整时才运行本地实验。</p></header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <section class="panel directory" aria-label="金工方法目录"><div class="section-head"><div><span class="eyebrow">METHOD LIBRARY</span><h2>研究方法</h2></div><span class="count">{{ visible.length }} 项</span></div><p v-if="directory.status === 'NOT_PUBLISHED'">研报提炼方法尚未审核发布；当前展示内置工具与公开研究原理。</p>
      <div class="filters"><input v-model="query" type="search" aria-label="搜索金工方法" placeholder="搜索方法名称和说明"><select v-model="kind" aria-label="方法类型"><option value="all">工具 / 方法 / 策略</option><option>工具</option><option>方法</option><option>策略</option></select><select v-model="category" aria-label="研究主题"><option value="all">全部研究主题</option><option v-for="item in directory.categories" :key="item">{{ item }}</option></select></div>
      <div class="method-list"><button v-for="item in visible" :key="item.id" type="button" :class="{ active: item.id === selected?.id }" @click="select(item)"><span><strong>{{ item.name }}</strong><small>{{ item.kind }} · {{ item.category }}</small></span><em>{{ item.implementationStatus === "RUNNABLE" ? "可检查" : "仅供阅读" }}</em></button><p v-if="!visible.length" class="empty">没有匹配的方法。新研报须经过逐页核验后才会进入目录。</p></div>
    </section>
    <section v-if="selected" class="panel method-detail" aria-label="方法说明"><span class="eyebrow">METHOD PRINCIPLE</span><div class="section-head"><h2>{{ selected.name }}</h2><span class="badge">{{ selected.implementationStatus === "RUNNABLE" ? "已实现 · 仍需检查数据" : "原理解读" }}</span></div><p v-if="selected.origin === 'PUBLIC_RESEARCH_OVERVIEW'">公开研究原理概览；尚未实现策略实验。</p><p class="summary">{{ selected.summary }}</p>
      <dl><dt>公式</dt><dd>{{ selected.formula || "尚缺完整可核对公式" }}</dd><dt>输入数据</dt><dd>{{ selected.requiredData.join("；") || "尚待确认" }}</dd><dt>假设</dt><dd>{{ selected.assumptions.join("；") || "尚待确认" }}</dd><dt>适用限制</dt><dd>{{ selected.limitations.join("；") || "尚待确认" }}</dd></dl>
      <div v-if="selected.blockReasons.length" class="blocked"><strong>当前不可运行</strong><ul><li v-for="reason in selected.blockReasons" :key="reason">{{ reason }}</li></ul></div>
    </section>
    <section v-if="selected" class="panel readiness" aria-label="数据就绪检查"><span class="eyebrow">DATA READINESS</span><h2>本机数据检查</h2><p>请明确指定标的和日期。检查只读取本机 Silver 日线；未通过时不会生成曲线。</p>
      <div class="run-form"><label>标的代码<input v-model="instrumentId" placeholder="例如 CN.SSE.STOCK.600000"></label><label>起始日<input v-model="start" type="date"></label><label>截至日<input v-model="end" type="date"></label><label v-if="selected.id === 'rolling-volatility'">窗口交易日<input v-model.number="windowDays" type="number" min="2" max="252" placeholder="自行填写"></label><button type="button" :disabled="busy" @click="check">检查可用性</button></div>
      <div v-if="gate" class="gate" :class="{ ready: gate.ready }"><strong>{{ gate.ready ? `数据通过 · ${gate.barCount} 个交易日` : "暂不可运行" }}</strong><span v-if="gate.ready">{{ gate.firstDay }} 至 {{ gate.lastDay }}</span><ul v-else><li v-for="reason in gate.reasons" :key="reason">{{ reason }}</li></ul></div>
      <button v-if="gate?.ready" type="button" class="run-button" :disabled="busy" @click="execute">运行本地价格统计</button>
    </section>
    <section class="panel result" aria-label="本地实验结果"><span class="eyebrow">LOCAL CALCULATION</span><h2>本地计算结果</h2><template v-if="run"><p>以 {{ run.firstDay }}—{{ run.lastDay }} 的 {{ run.barCount }} 条本机日线计算；这是价格统计，不代表研报或实盘收益。</p><div class="metrics"><div v-for="(value, key) in run.metrics" :key="key"><small>{{ metricLabels[key] || key }}</small><strong>{{ displayMetric(String(key), value) }}</strong></div></div><div ref="chartElement" class="chart" role="img" aria-label="本地价格统计图"></div><p class="limitations">{{ run.limitations.join("；") }}</p><code>运行编号 {{ run.runId }}</code></template><p v-else class="empty">尚无本地结果。先选择方法并通过数据检查。</p></section>
  </section>
</template>

<style scoped>
.quant-page{max-width:1500px;margin:0 auto;padding:8px 10px 40px;display:grid;gap:17px}.heading{padding-bottom:20px;border-bottom:1px solid var(--ml-divider)}.eyebrow{font-size:10px;letter-spacing:.16em;font-weight:700;color:var(--ml-accent)}h1{font-size:30px;margin:5px 0}h2{font-size:19px;margin:5px 0}p{font-size:13px;line-height:1.7;color:var(--ml-text-secondary)}.panel{background:var(--ml-surface);border:1px solid var(--ml-divider);border-radius:var(--ml-radius-panel);padding:20px 24px;box-shadow:0 4px 18px var(--ml-shadow-soft,var(--ml-divider))}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.count,.badge{font-size:11px;color:var(--ml-text-secondary);border:1px solid var(--ml-divider);border-radius:20px;padding:5px 10px}.filters,.run-form{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}.filters input{min-width:240px;flex:1}.filters input,.filters select,.run-form input{background:var(--ml-surface);border:1px solid var(--ml-divider);color:var(--ml-text-primary);border-radius:8px;padding:9px 10px}.run-form label{display:grid;gap:5px;font-size:11px;color:var(--ml-text-secondary)}.run-form input{min-width:155px}.run-form button,.run-button{align-self:end;background:var(--ml-accent);border:0;color:#08251d;border-radius:8px;padding:10px 15px;cursor:pointer;font-weight:650}.run-form button:disabled,.run-button:disabled{opacity:.5;cursor:default}.method-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:9px;max-height:380px;overflow:auto}.method-list button{text-align:left;background:transparent;color:var(--ml-text-primary);border:1px solid var(--ml-divider);border-radius:10px;padding:13px;display:flex;justify-content:space-between;gap:10px;cursor:pointer}.method-list button:hover,.method-list button.active{border-color:var(--ml-accent);background:color-mix(in srgb,var(--ml-accent) 8%,var(--ml-surface))}.method-list small{display:block;color:var(--ml-text-secondary);margin-top:5px}.method-list em{font-size:10px;font-style:normal;white-space:nowrap;color:var(--ml-accent)}dl{display:grid;grid-template-columns:100px 1fr;gap:11px 18px;margin:18px 0;font-size:13px}dt{color:var(--ml-text-secondary)}dd{margin:0;line-height:1.7}.blocked,.gate{background:color-mix(in srgb,#c88951 10%,var(--ml-surface));border:1px solid var(--ml-divider);border-radius:8px;padding:12px 16px;font-size:12px}.gate.ready{background:color-mix(in srgb,var(--ml-accent) 10%,var(--ml-surface))}.blocked ul,.gate ul{margin:7px 0;padding-left:20px}.gate span{margin-left:12px;color:var(--ml-text-secondary)}.run-button{margin-top:12px}.metrics{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:20px 0}.metrics div{border:1px solid var(--ml-divider);border-radius:9px;padding:12px}.metrics small{display:block;color:var(--ml-text-secondary)}.metrics strong{display:block;font-size:19px;margin-top:6px}.chart{height:320px;width:100%}.limitations,.empty{font-size:12px}.result code{font-size:11px;color:var(--ml-text-secondary)}.error{color:#e49393;border:1px solid #a55;border-radius:8px;padding:10px 15px}@media(max-width:680px){.panel{padding:16px}.filters>*{width:100%}dl{grid-template-columns:1fr;gap:3px}.method-list{grid-template-columns:1fr}}
</style>
