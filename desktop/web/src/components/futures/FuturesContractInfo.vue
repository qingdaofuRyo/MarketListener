<script setup lang="ts">
import { computed, ref } from "vue";
import type { FuturesContractInfoProduct, FuturesContractInfoResponse } from "../../domain/futures";

const props = defineProps<{ payload: FuturesContractInfoResponse }>();
const exchange = ref("");
const search = ref("");
const expanded = ref("");
const exchanges = ["CFFEX", "SHFE", "INE", "DCE", "CZCE", "GFEX"];
const exchangeNames: Record<string, string> = { CFFEX: "中金所", SHFE: "上期所", INE: "上期能源", DCE: "大商所", CZCE: "郑商所", GFEX: "广期所" };
const rows = computed(() => props.payload.products.filter(product => {
  if (exchange.value && product.exchange !== exchange.value) return false;
  const query = search.value.trim().toLocaleLowerCase();
  return !query || `${product.name} ${product.key}`.toLocaleLowerCase().includes(query);
}));
function listingText(product: FuturesContractInfoProduct): string {
  const listing = product.listing;
  if (!listing?.date || listing.status !== "PASS") return "待官网核验";
  return listing.precision === "DAY" ? listing.date
    : listing.precision === "MONTH" ? listing.date.slice(0, 7) + "（月级）"
      : listing.date.slice(0, 4) + "（年级）";
}
function field(product: FuturesContractInfoProduct, ...names: string[]): string {
  const values = product.currentSpec?.fields || {};
  return names.map(name => values[name]).find(Boolean) || "—";
}
</script>

<template>
  <section class="contract-info panel" data-test="futures-contract-info">
    <header><div class="contract-heading"><h2>期货合约信息</h2><span class="counts">{{ rows.length }} / {{ payload.products.length }} 个品种</span></div>
      <div class="filters"><select v-model="exchange" aria-label="合约信息交易所"><option value="">全部交易所</option><option v-for="item in exchanges" :key="item" :value="item">{{ exchangeNames[item] || item }}</option></select><input v-model="search" type="search" aria-label="搜索期货合约信息" placeholder="搜索品种或代码" /></div>
    </header>
    <div class="table-scroll" tabindex="0" role="region" aria-label="期货合约信息表，可横向与纵向滚动"><table><thead><tr><th>交易所</th><th>期货品种</th><th>上市交易日</th><th>交易单位 / 合约乘数</th><th>报价单位</th><th>最小变动价位</th><th>涨跌停板幅度</th><th>合约月份</th><th>交易时间</th><th>最后交易日</th><th>最后交割日</th><th>最低交易保证金</th></tr></thead>
      <tbody v-for="product in rows" :key="product.key"><tr class="product-row" :class="{ chosen: expanded === product.key }" tabindex="0" role="button" :aria-expanded="expanded === product.key" @click="expanded = expanded === product.key ? '' : product.key" @keydown.enter="expanded = expanded === product.key ? '' : product.key" @keydown.space.prevent="expanded = expanded === product.key ? '' : product.key">
        <td>{{ exchangeNames[product.exchange] || product.exchange }}</td><td><strong>{{ product.name }}</strong><small>{{ product.productCode }}</small></td><td>{{ listingText(product) }}</td><td>{{ field(product, '交易单位', '合约乘数') }}</td><td>{{ field(product, '报价单位') }}</td><td>{{ field(product, '最小变动价位') }}</td><td class="term-cell">{{ field(product, '涨跌停板幅度', '每日价格波动限制', '每日价格最大波动限制') }}</td><td class="term-cell">{{ field(product, '合约交割月份', '合约月份') }}</td><td class="term-cell">{{ field(product, '交易时间') }}</td><td class="term-cell">{{ field(product, '最后交易日') }}</td><td class="term-cell">{{ field(product, '最后交割日', '交割日期') }}</td><td class="term-cell">{{ field(product, '最低交易保证金') }}</td>
      </tr><tr v-if="expanded === product.key" class="detail-row"><td colspan="12"><div class="details"><div class="detail-heading"><div><h3>{{ product.name }}（{{ product.productCode }}）</h3><p>品种上市交易日：{{ listingText(product) }} · 本项目官网日行情首见日：{{ product.firstObservedDay || '—' }}</p></div><a v-if="product.listing?.sourceUrl" :href="product.listing.sourceUrl" target="_blank" rel="noopener noreferrer">上市日期来源 ↗</a></div>
        <p v-if="product.currentSpec?.sourceKind === 'OFFICIAL_CZCE_BUSINESS_RULES_PDF'" class="historical-note">以下来自官网业务细则；当前完整合约文本及其修订状态尚待核验。</p>
        <p v-if="product.currentSpec?.sourceKind === 'OFFICIAL_CZCE_HISTORICAL_CONTRACT_PDF'" class="historical-note">以下是已存档的历史合约文本；现行版本尚待官网核验。</p>
        <p v-if="product.currentSpec?.sourceKind === 'OFFICIAL_CHROME_DOM_FIELDS'" class="historical-note">以下为已打开的郑商所官网页面中直接读取的期货合约表；校验值对应结构化表格，非HTTP响应原件。</p>
        <p v-if="product.currentSpec?.sourceKind === 'OFFICIAL_CZCE_CONTRACT_DOCX'" class="historical-note">郑商所2026年修订净稿，自2026年9月16日起适用于RS2701及后续合约；更早月份合约按原版本执行。</p>
        <div class="term-grid"><div v-for="[name, value] in Object.entries(product.currentSpec?.fields || {})" :key="name" class="term"><span>{{ name }}</span><strong>{{ value }}</strong></div></div>
        <div v-if="!product.currentSpec" class="detail-empty">该品种尚无可读取的官网合约条款。</div>
        <details v-if="product.monthContractListings?.length" class="month-listings"><summary>具体月份合约挂牌日（{{ product.monthContractListings.length }} 个）</summary><div class="month-grid"><span v-for="month in product.monthContractListings" :key="month.contractCode"><strong>{{ month.contractCode }}</strong><time>{{ month.listedOn }}</time></span></div></details>
        <div class="versions"><span>官网来源版本</span><a v-for="version in product.versions" :key="version.payloadSha256" :href="version.sourceUrl" target="_blank" rel="noopener noreferrer">{{ version.publishedOn || '发布日期未确认' }} · {{ version.sourceKind === 'OFFICIAL_CHROME_DOM_FIELDS' ? 'Chrome期货合约表' : version.qualityStatus === 'PASS' ? '合约文本' : version.sourceKind === 'OFFICIAL_CZCE_BUSINESS_RULES_PDF' ? '业务细则' : version.sourceKind === 'OFFICIAL_CZCE_HISTORICAL_CONTRACT_PDF' ? '历史合约文本' : '部分字段' }} ↗</a></div>
        <p v-if="product.error" class="source-error">本次官网读取：{{ product.error }}</p>
      </div></td></tr></tbody>
    </table></div>
    <p v-if="!rows.length" class="empty">没有匹配的期货品种。</p>
  </section>
</template>

<style scoped>
.panel { padding: 20px; border: 1px solid var(--ml-divider); border-radius: 12px; background: var(--ml-surface); }
.contract-info { min-width: 0; }
header,.filters,.detail-heading,.versions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h2,h3,p { margin: 0; }h2 { font-size: 16px; font-weight: 650; }.counts { color: var(--ml-text-secondary); font: 11px ui-monospace,Consolas,monospace; }
.contract-heading { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
select,input { min-height: 34px; padding: 6px 10px; border: 1px solid var(--ml-divider); border-radius: 8px; background: var(--ml-surface-elevated); color: var(--ml-text-primary); font-size: 12px; transition: border-color 150ms; }
select:hover,input:hover { border-color: color-mix(in srgb,var(--ml-accent) 35%,var(--ml-divider)); }
select:focus-visible,input:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
input { width: 210px; }
.table-scroll { max-height: 580px; margin-top: 16px; overflow: auto; border: 1px solid var(--ml-divider); border-radius: 9px; scrollbar-width: thin; scrollbar-color: var(--ml-chart-secondary) transparent; }
.table-scroll:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
table { width: 100%; min-width: 1940px; border-collapse: collapse; font-size: 12px; }
th { position: sticky; top: 0; z-index: 2; padding: 11px 10px; background: var(--ml-surface-selected); color: var(--ml-text-secondary); text-align: left; font-weight: 600; }
td { padding: 10px; border-top: 1px solid var(--ml-divider); vertical-align: middle; }
th:first-child,.product-row td:first-child { position: sticky; left: 0; width: 84px; min-width: 84px; max-width: 84px; box-sizing: border-box; }
th:nth-child(2),.product-row td:nth-child(2) { position: sticky; left: 84px; min-width: 132px; box-shadow: 1px 0 var(--ml-divider); }
th:first-child,th:nth-child(2) { z-index: 4; }
.product-row td:first-child,.product-row td:nth-child(2) { z-index: 1; background: var(--ml-surface); }
.product-row:hover td:first-child,.product-row:hover td:nth-child(2),.product-row.chosen td:first-child,.product-row.chosen td:nth-child(2) { background: var(--ml-surface-selected); }
.term-cell { min-width: 125px; max-width: 245px; line-height: 1.5; overflow-wrap: anywhere; }
.product-row { cursor: pointer; transition: background 140ms; }.product-row:hover,.product-row.chosen { background: var(--ml-surface-selected); }.product-row:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: -2px; }
.product-row td:nth-child(2) { white-space: nowrap; }.product-row strong { color: var(--ml-text-primary); }.product-row small { display: block; margin-top: 2px; color: var(--ml-text-secondary); font: 11px ui-monospace,Consolas,monospace; }
.detail-row td { padding: 0; }.details { padding: 16px; background: var(--ml-surface-elevated); }.details h3 { font-size: 15px; }.details p { color: var(--ml-text-secondary); font-size: 12px; }.details a { color: var(--ml-accent); text-decoration: none; }.details a:hover { text-decoration: underline; }
.term-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 1px; margin-top: 13px; border: 1px solid var(--ml-divider); background: var(--ml-divider); }.term { display: grid; grid-template-columns: 150px minmax(0,1fr); gap: 9px; padding: 10px; background: var(--ml-surface); }.term span { color: var(--ml-text-secondary); }.term strong { font-weight: 500; overflow-wrap: anywhere; }
.historical-note { margin-top: 12px; }
.versions { justify-content: flex-start; margin-top: 13px; font-size: 12px; }.versions span { color: var(--ml-text-secondary); }.source-error { margin-top: 12px; overflow-wrap: anywhere; }.detail-empty,.empty { padding: 24px; text-align: center; color: var(--ml-text-secondary); }
.month-listings { margin-top: 12px; color: var(--ml-text-primary); font-size: 12px; }.month-listings summary { cursor: pointer; }.month-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(145px,1fr)); gap: 5px; margin-top: 10px; }.month-grid span { display: flex; justify-content: space-between; gap: 8px; padding: 6px; border-radius: 5px; background: var(--ml-surface-selected); }.month-grid time { color: var(--ml-text-secondary); }
@media (max-width: 900px) { .term-grid { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .panel { padding: 14px; }.filters { width: 100%; }.filters input { flex: 1; min-width: 120px; }.term { grid-template-columns: 110px minmax(0,1fr); }.contract-heading { gap: 8px; } }
@media (prefers-reduced-motion: reduce) { select,input,.product-row { transition: none; } }
</style>
