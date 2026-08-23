<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiGet, apiPut, formatAssetType, formatField, formatMarket, formatNumber, formatPeriod, formatStatus, formatTime } from "../domain/api";

interface Provider {
  providerId: string;
  name: string;
  type: string;
  access: string;
  endpoint: string;
  authentication: string;
  implemented: boolean;
  configured: boolean;
  priority: number;
  enabled: boolean;
  markets: string[];
  assetTypes: string[];
  periods: string[];
  fields: string[];
  fieldSchema: LocalField[];
  fieldNotes?: string | null;
  status: string;
}
interface InventoryItem {
  categoryKey: string;
  market: string;
  assetType: string;
  period: string;
  instruments: number;
  rows: number;
  earliestBarAt?: string;
  latestBarAt?: string;
  lastUpdatedAt?: string;
  sources: string[];
  sourceDetails: Array<{ providerId: string; name: string; endpoint?: string | null; status: string; periods: string[]; fields: string[]; fieldNotes?: string | null }>;
  quality: Record<string, number>;
  fieldCompleteness: Record<string, number>;
  fieldCoverageSamples: number;
  partitions: number;
  rowCountMode: string;
}
interface LocalField { name: string; type: string; nullable: boolean; storage: string }
interface LocalTable {
  tableId: string;
  name: string;
  kind: string;
  storage: string;
  dataSources: string[];
  rows: number;
  rowCountMode: string;
  partitions: number;
  updatedAt?: string | null;
  fields: LocalField[];
  columnCount: number;
}
interface RegisteredDataset {
  datasetId: string;
  name: string;
  market: string;
  assetType: string;
  frequency: string;
  source: string;
  rows?: number | null;
  rowCountMode: string;
  partitions?: number | null;
  registeredAt?: string | null;
  primaryKey: string[];
  fields: LocalField[];
  description: string;
}
interface Preference { primary?: string | null; fallback1?: string | null; fallback2?: string | null }
interface InventoryPayload {
  inventory: InventoryItem[];
  tables: LocalTable[];
  datasets: RegisteredDataset[];
  preferences: Record<string, Preference>;
  metadata: { mode: string; rowCounts: string; fieldCoverage: string; scansSilverRows: boolean };
  summary: { categories: number; rows: number; instruments: number; tables: number; datasets: number };
}

const payload = ref<InventoryPayload>({
  inventory: [], tables: [], datasets: [], preferences: {},
  metadata: { mode: "LIGHTWEIGHT_MANIFEST", rowCounts: "", fieldCoverage: "", scansSilverRows: false },
  summary: { categories: 0, rows: 0, instruments: 0, tables: 0, datasets: 0 },
});
const providers = ref<Provider[]>([]);
const preferences = ref<Record<string, Preference>>({});
const inventoryLoading = ref(false);
const providersLoading = ref(false);
const loading = computed(() => inventoryLoading.value || providersLoading.value);
const saving = ref(false);
const error = ref("");
const custom = ref<Record<string, string>>({});
const providerOptions = computed(() => providers.value.map((item) => ({ value: item.providerId, label: `${item.name} · P${item.priority} · ${formatStatus(item.status)}` })));
const minuteFieldRules = [
  { name: "开盘价、收盘价、最低价、最高价", kind: "原始 K 线", detail: "所有已入库分钟 K 线的 OHLC 价格。" },
  { name: "成交量", kind: "原始 K 线", detail: "有分钟行情即写入；单位依市场/合约而定。" },
  { name: "成交额", kind: "来源相关", detail: "pytdx、BaoStock 等可提供；期货通本地 .lc5 文件不提供。" },
  { name: "持仓量", kind: "期货/商品指数", detail: "只对期货与商品指数写入；股票、ETF、普通指数不适用。" },
  { name: "结算价", kind: "日线期货", detail: "当前期货通仅在日线 .day 文件提供，分钟线不提供。" },
  { name: "涨跌额、涨跌幅、振幅", kind: "可派生", detail: "由本周期收盘价、前一根收盘价及最高/最低价计算；未当作期货通分钟线原始字段。" },
  { name: "换手率、总量", kind: "未统一落库", detail: "换手率属于股票/ETF 行情快照；“总量”口径不统一，当前分钟 K 线未保存累计值。" },
  { name: "沉淀资金", kind: "期货派生", detail: "不是原始 K 线字段；需持仓量、价格、合约乘数、保证金比例后计算。" },
] as const;

function preferenceFor(key: string): Preference {
  return preferences.value[key] ?? (preferences.value[key] = { primary: null, fallback1: null, fallback2: null });
}
function presentFields(item: InventoryItem): string[] {
  return Object.entries(item.fieldCompleteness)
    .filter(([, completeness]) => completeness > 0)
    .map(([field]) => field);
}
function missingFields(item: InventoryItem): string[] {
  return Object.entries(item.fieldCompleteness)
    .filter(([, completeness]) => completeness === 0)
    .map(([field]) => field);
}
function quality(item: InventoryItem): string {
  const values = Object.entries(item.quality).map(([key, value]) => `${formatStatus(key)} ${value}`).join(" · ");
  return values || "暂无最新记录质量样本";
}
function rowCountLabel(mode: string): string {
  if (mode === "MANIFEST_EXACT") return "清单精确值";
  if (mode === "CATALOG_ESTIMATE") return "目录估算值";
  return "尚未映射";
}
function addCustom(key: string): void {
  const value = custom.value[key]?.trim();
  if (!value) return;
  preferenceFor(key).primary = value;
  custom.value[key] = "";
}
async function load(): Promise<void> {
  inventoryLoading.value = true; providersLoading.value = true; error.value = "";
  const [inventoryResult, providerResult] = await Promise.allSettled([
    apiGet<InventoryPayload>("/api/data-sources/inventory", undefined, { force: true }),
    apiGet<{ items: Provider[] }>("/api/data-sources/providers", undefined, { force: true }),
  ]);
  if (inventoryResult.status === "fulfilled") {
    payload.value = inventoryResult.value;
    preferences.value = JSON.parse(JSON.stringify(inventoryResult.value.preferences ?? {})) as Record<string, Preference>;
  } else {
    error.value = inventoryResult.reason instanceof Error ? inventoryResult.reason.message : "本地数据目录加载失败";
  }
  inventoryLoading.value = false;
  if (providerResult.status === "fulfilled") providers.value = providerResult.value.items;
  else error.value = [error.value, providerResult.reason instanceof Error ? providerResult.reason.message : "Provider 注册表加载失败"].filter(Boolean).join("；");
  providersLoading.value = false;
}
async function save(): Promise<void> {
  saving.value = true; error.value = "";
  try { const data = await apiPut<{ preferences: Record<string, Preference> }>("/api/data-sources", { preferences: preferences.value }); preferences.value = data.preferences; }
  catch (reason) { error.value = reason instanceof Error ? reason.message : "数据源配置保存失败"; }
  finally { saving.value = false; }
}
onMounted(() => void load());
</script>

<template>
  <section>
    <div class="page-heading">
      <div><h1 class="page-title">数据源</h1><p class="page-note">只展示本机 Silver 存储和当前代码中真实实现的 Provider；未配置的付费/授权来源不会被标记为可用。</p></div>
      <div><el-button :loading="loading" @click="load">刷新盘点</el-button><el-button type="primary" :loading="saving" data-test="data-sources-save" @click="save">保存路由配置</el-button></div>
    </div>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />
    <section class="overview-strip"><div class="metric compact"><span>本地表 / 数据集</span><strong>{{ payload.summary.tables }} / {{ payload.summary.datasets }}</strong></div><div class="metric compact"><span>K 线记录</span><strong>{{ formatNumber(payload.summary.rows) }}</strong></div><div class="metric compact"><span>已入库行情标的（非全市场）</span><strong>{{ formatNumber(payload.summary.instruments) }}</strong></div></section>
    <section class="panel data-source-panel">
      <div class="panel-heading">
        <div><h2>本地数据库浏览器</h2><p class="page-note">展示物理表、Parquet 数据集、字段名与类型。{{ payload.metadata.rowCounts }}；页面不会扫描完整 Silver 行情明细。</p></div>
        <el-tag type="success" effect="plain">轻量元数据</el-tag>
      </div>
      <el-table :data="payload.tables" v-loading="inventoryLoading" empty-text="当前没有已登记的本地表" data-test="local-database-tables">
        <el-table-column type="expand">
          <template #default="scope">
            <div class="schema-browser">
              <p><strong>{{ scope.row.tableId }}</strong> · {{ scope.row.storage }}</p>
              <el-table :data="scope.row.fields" size="small" border empty-text="没有字段元数据">
                <el-table-column prop="name" label="字段名" min-width="170" />
                <el-table-column prop="type" label="字段类型" min-width="150" />
                <el-table-column label="可空" width="80"><template #default="fieldScope">{{ fieldScope.row.nullable ? "是" : "否" }}</template></el-table-column>
                <el-table-column prop="storage" label="实际存储位置" min-width="220" />
              </el-table>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="表 / 数据集" min-width="190"><template #default="scope"><strong>{{ scope.row.name }}</strong><small>{{ scope.row.tableId }} · {{ scope.row.kind }}</small></template></el-table-column>
        <el-table-column label="数据源" min-width="160"><template #default="scope">{{ scope.row.dataSources.join(" / ") || "本地派生 / 未登记" }}</template></el-table-column>
        <el-table-column label="当前行数" min-width="140"><template #default="scope"><strong>{{ formatNumber(scope.row.rows) }}</strong><small>{{ rowCountLabel(scope.row.rowCountMode) }}</small></template></el-table-column>
        <el-table-column label="分区 / 字段" min-width="120"><template #default="scope">{{ formatNumber(scope.row.partitions) }} / {{ scope.row.columnCount }}</template></el-table-column>
        <el-table-column label="更新时间" min-width="170"><template #default="scope">{{ formatTime(scope.row.updatedAt) }}</template></el-table-column>
      </el-table>
    </section>
    <section class="panel data-source-panel">
      <h2>数据集注册目录</h2>
      <el-table :data="payload.datasets" v-loading="inventoryLoading" empty-text="catalog.duckdb 中没有数据集注册项" data-test="registered-datasets">
        <el-table-column type="expand">
          <template #default="scope">
            <div class="schema-browser">
              <p>{{ scope.row.description || "没有补充说明" }}<small>主键：{{ scope.row.primaryKey.join(" + ") || "未登记" }}</small></p>
              <el-table :data="scope.row.fields" size="small" border>
                <el-table-column prop="name" label="字段名" min-width="170" />
                <el-table-column prop="type" label="声明类型" min-width="150" />
              </el-table>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="数据集" min-width="210"><template #default="scope"><strong>{{ scope.row.name }}</strong><small>{{ scope.row.datasetId }}</small></template></el-table-column>
        <el-table-column label="市场 / 类型 / 周期" min-width="190"><template #default="scope">{{ formatMarket(scope.row.market) }} · {{ formatAssetType(scope.row.assetType) }}<small>{{ scope.row.frequency }}</small></template></el-table-column>
        <el-table-column prop="source" label="登记数据源" min-width="210" />
        <el-table-column label="当前行数 / 分区" min-width="150"><template #default="scope"><template v-if="scope.row.rows !== null && scope.row.rows !== undefined">{{ formatNumber(scope.row.rows) }} / {{ formatNumber(scope.row.partitions) }}</template><template v-else>尚未映射物理表</template><small>{{ rowCountLabel(scope.row.rowCountMode) }}</small></template></el-table-column>
        <el-table-column label="登记时间" min-width="170"><template #default="scope">{{ formatTime(scope.row.registeredAt) }}</template></el-table-column>
      </el-table>
    </section>
    <section class="panel data-source-panel">
      <h2>K 线字段口径</h2>
      <p class="page-note">所有非 TickDB K 线先标准化再入库。成交量单位随市场而定（股票通常为手、期货为手/张），缺失字段保持空值，绝不以 0 补齐。</p>
      <div class="field-legend" data-test="kline-field-legend">
        <el-tag effect="plain">开/高/低/收：OHLC 价格</el-tag><el-tag effect="plain">成交量：volume</el-tag><el-tag effect="plain">成交额：amount</el-tag><el-tag effect="plain">持仓量：open_interest（期货为主）</el-tag><el-tag effect="plain">结算价：settlement（日线期货为主）</el-tag><el-tag effect="plain">涨跌幅/振幅：pct_change / amplitude</el-tag>
      </div>
    </section>
    <section class="panel data-source-panel">
      <h2>分钟 K 线字段规则</h2>
      <div class="field-policy-grid" data-test="minute-kline-field-rules">
        <article v-for="item in minuteFieldRules" :key="item.name" class="field-policy-card">
          <strong>{{ item.name }}</strong><el-tag size="small" effect="plain">{{ item.kind }}</el-tag><small>{{ item.detail }}</small>
        </article>
      </div>
    </section>
    <section class="panel data-source-panel"><h2>本地数据类别与路由</h2><el-table :data="payload.inventory" v-loading="inventoryLoading" empty-text="K 线清单尚未建立或本地没有行情数据" data-test="data-source-inventory"><el-table-column label="类别" min-width="190"><template #default="scope"><strong>{{ formatMarket(scope.row.market) }} · {{ formatAssetType(scope.row.assetType) }}</strong><small>{{ formatPeriod(scope.row.period) }} · {{ formatNumber(scope.row.partitions) }} 个文件分区</small></template></el-table-column><el-table-column label="实际覆盖" min-width="156"><template #default="scope">{{ scope.row.instruments }} 标的 · {{ formatNumber(scope.row.rows) }} 行<small>{{ formatTime(scope.row.earliestBarAt) }} 至 {{ formatTime(scope.row.latestBarAt) }}</small><small>行数：{{ rowCountLabel(scope.row.rowCountMode) }}</small></template></el-table-column><el-table-column label="实际来源 / 接口 / 质量" min-width="300"><template #default="scope">{{ scope.row.sources.join(" / ") || "来源待清单补全" }}<small v-for="detail in scope.row.sourceDetails" :key="detail.providerId">{{ detail.name }}（{{ detail.providerId }}）· {{ detail.endpoint || "未注册接口" }} · {{ formatStatus(detail.status) }}</small><small>{{ quality(scope.row) }} · {{ formatTime(scope.row.lastUpdatedAt) }}</small></template></el-table-column><el-table-column label="字段样本" min-width="280"><template #default="scope"><div class="field-tags"><el-tag v-for="field in presentFields(scope.row)" :key="field" size="small" type="success" effect="plain">{{ formatField(field) }} {{ Math.round(scope.row.fieldCompleteness[field] * 100) }}%</el-tag><el-tag v-for="field in missingFields(scope.row)" :key="field" size="small" type="info" effect="plain">{{ formatField(field) }}：样本无值</el-tag></div><small>基于 {{ formatNumber(scope.row.fieldCoverageSamples) }} 个标的的最新记录，不扫描完整历史。</small></template></el-table-column><el-table-column label="主 / 备数据源" min-width="330"><template #default="scope"><div class="source-routing"><el-select v-model="preferenceFor(scope.row.categoryKey).primary" clearable placeholder="主要数据源"><el-option v-for="item in providerOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select><el-select v-model="preferenceFor(scope.row.categoryKey).fallback1" clearable placeholder="备用数据源 1"><el-option v-for="item in providerOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select><el-select v-model="preferenceFor(scope.row.categoryKey).fallback2" clearable placeholder="备用数据源 2"><el-option v-for="item in providerOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select><div class="custom-source"><el-input v-model="custom[scope.row.categoryKey]" placeholder="自定义数据源代号" /><el-button @click="addCustom(scope.row.categoryKey)">设为主源</el-button></div></div></template></el-table-column></el-table></section>
    <section class="panel data-source-panel"><h2>已实现 Provider 注册表</h2><el-table :data="providers" v-loading="providersLoading" empty-text="当前代码中没有已注册 Provider" data-test="provider-registry"><el-table-column prop="name" label="Provider" min-width="130"><template #default="scope"><strong>{{ scope.row.name }}</strong><small>{{ scope.row.providerId }} · {{ formatStatus(scope.row.status) }}</small></template></el-table-column><el-table-column label="访问方式 / 实际接口" min-width="300"><template #default="scope">{{ scope.row.access }}<small>{{ scope.row.endpoint }}</small></template></el-table-column><el-table-column label="可写入的标准字段 / 类型" min-width="300"><template #default="scope">{{ scope.row.markets.map(formatMarket).join("/") }} · {{ scope.row.assetTypes.map(formatAssetType).join("/") }}<div class="field-tags"><el-tag v-for="field in scope.row.fieldSchema" :key="field.name" size="small" effect="plain">{{ formatField(field.name) }} · {{ field.type }}</el-tag></div><small>{{ scope.row.periods.map(formatPeriod).join("/") }}。{{ scope.row.fieldNotes || "未登记额外字段说明。" }}</small></template></el-table-column><el-table-column label="认证 / 配置" min-width="160"><template #default="scope">{{ scope.row.authentication }}<small>{{ scope.row.configured ? "当前可配置" : "未配置，不能作为可用来源" }}</small></template></el-table-column></el-table></section>
  </section>
</template>

<style scoped>
.field-legend,
.field-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.field-legend {
  margin-top: 12px;
}

.panel-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.panel-heading h2,
.panel-heading p,
.schema-browser p {
  margin: 0;
}

.panel-heading p {
  margin-top: 5px;
}

.schema-browser {
  padding: 8px 16px 16px;
}

.schema-browser p {
  margin-bottom: 10px;
}

.schema-browser p small {
  display: block;
  margin-top: 4px;
  color: var(--ml-text-secondary);
}

.field-tags {
  margin-bottom: 6px;
}

.field-policy-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 10px;
}

.field-policy-card {
  display: grid;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
</style>
