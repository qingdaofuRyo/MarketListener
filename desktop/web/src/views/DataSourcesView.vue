<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { apiGet, formatAssetType, formatMarket as legacyMarketName, formatNumber, formatPeriod, formatTime } from "../domain/api";
import { formatBytes, occupiedSources, share, type InventoryPayload } from "../domain/sourceDashboard";
const data = ref<InventoryPayload | null>(null);
const loading = ref(false);
const error = ref("");
let disposed = false;
// CN includes futures as well as equities in the inventory contract.
const formatMarket = (market: string) => market === "CN" ? "中国内地" : legacyMarketName(market);
const inventory = computed(() => data.value?.inventory.filter(item => item.rows > 0) ?? []);
const sources = computed(() => data.value ? occupiedSources(data.value) : []);
const groups = computed(() => data.value?.storage?.groups ?? []);
const rows = computed(() => inventory.value.reduce((total, item) => total + item.rows, 0));
const latest = computed(() => inventory.value.map(item => item.latestBarAt).filter((date): date is string => !!date).sort().at(-1));
async function load() {
  if (loading.value) return;
  loading.value = true; error.value = "";
  try {
    const result = await apiGet<InventoryPayload>("/api/data-sources/inventory", undefined, {force: true});
    if (!disposed) data.value = result;
  } catch { if (!disposed) error.value = "本地数据盘点加载失败；不能据此判断数据为空。请重试。"; }
  finally { loading.value = false; }
}
onMounted(() => void load());
onBeforeUnmount(() => { disposed = true; });
</script>
<template>
  <section class="source-dashboard">
    <div class="panel-title"><div><h1 class="page-title">数据源</h1><p class="page-note">本地数据盘点 · 沿用系统现有来源，不在此页手动切换</p></div><el-button :loading="loading" @click="load">刷新盘点</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <p v-if="loading" role="status">正在读取本地数据目录…</p>
    <el-alert v-if="data?.storage?.available === false" title="本地行情清单尚未就绪，暂不能确认库存数量及来源。请稍后刷新。" type="warning" :closable="false" />
    <template v-if="data && data.storage?.available !== false">
      <div class="source-metrics">
        <article class="panel"><span>已登记行情文件容量</span><strong>{{ formatBytes(data.storage?.available ? data.storage.bytes : null) }}</strong><small>{{ data.storage?.files ?? '—' }} 个可读取文件 · 不含原始下载和缓存</small></article>
        <article class="panel"><span>K线记录</span><strong>{{ formatNumber(rows) }}</strong><small>来源于已登记文件的行数</small></article>
        <article class="panel"><span>标的数量</span><strong>{{ formatNumber(data.summary.instruments) }}</strong><small>跨周期去重</small></article>
        <article class="panel"><span>最新行情日期</span><strong class="date">{{ formatTime(latest) }}</strong><small>全库最新一条，不代表所有数据已更新</small></article>
      </div>
      <el-alert v-if="data.storage?.missingFiles" type="warning" :closable="false" :title="`有 ${data.storage.missingFiles} 个登记文件无法读取，容量仅统计可读取部分。`" />
      <div class="source-panels">
        <section class="panel"><h2>空间用在哪里</h2><p class="page-note">已登记行情文件的逻辑字节数，不等于压缩或稀疏文件的磁盘占用</p>
          <p v-if="!groups.length">暂无可统计的文件容量</p>
          <div v-for="group in groups" :key="group.market + group.assetType + group.period" class="inventory-bar">
            <div><span>{{ group.market === 'SHARED' ? '跨分类共享文件' : formatMarket(group.market) + ' · ' + formatAssetType(group.assetType) + ' · ' + formatPeriod(group.period) }}</span><strong>{{ formatBytes(group.bytes) }}</strong></div>
            <meter min="0" max="100" :value="share(group.bytes, data.storage?.bytes ?? 0)" :aria-label="formatMarket(group.market) + '容量占比'" />
            <small>{{ group.files }} 个文件 · 文件更新 {{ formatTime(group.updatedAt) }}</small>
          </div>
        </section>
        <section class="panel"><h2>数据类型与周期</h2><p class="page-note">按K线条数比较，不代表容量占比</p>
          <p v-if="!inventory.length">尚无已登记的K线数据</p>
          <div v-for="item in inventory" :key="item.categoryKey" class="inventory-bar">
            <div><span>{{ formatMarket(item.market) }} · {{ formatAssetType(item.assetType) }} · {{ formatPeriod(item.period) }}</span><strong>{{ formatNumber(item.rows) }} 条</strong></div>
            <meter min="0" max="100" :value="share(item.rows, rows)" :aria-label="formatAssetType(item.assetType) + '记录占比'" />
          </div>
        </section>
      </div>
      <section class="panel"><h2>已有行情的来源 <small>（{{ sources.length }}）</small></h2><p class="page-note">只展示最新行情记录中有来源证据的接口，不展示仅配置或登记过的来源。此处不推断全历史的来源占比。</p>
        <div class="source-cards"><article v-for="source in sources" :key="source.id"><h3>{{ source.name }}</h3><p>{{ source.categories.size }} 项行情分类有记录证据</p><details><summary>来源标识</summary><code>{{ source.id }}</code></details></article></div>
        <p v-if="!sources.length">尚无可确认的来源记录</p>
      </section>
      <section class="panel"><h2>数据覆盖与更新</h2><p class="page-note">起止日期不等于期间连续无缺口；以下“采集更新”为记录元数据，不等于磁盘文件修改日期。</p>
        <el-table :data="inventory" max-height="560" empty-text="暂无已登记行情">
          <el-table-column label="市场 / 类型 / 周期" min-width="230"><template #default="{row}">{{ formatMarket(row.market) }} / {{ formatAssetType(row.assetType) }} / {{ formatPeriod(row.period) }}</template></el-table-column>
          <el-table-column label="标的" width="100"><template #default="{row}">{{ formatNumber(row.instruments) }}</template></el-table-column>
          <el-table-column label="K线条数" width="140"><template #default="{row}">{{ formatNumber(row.rows) }}</template></el-table-column>
          <el-table-column label="最早行情" min-width="185"><template #default="{row}">{{ formatTime(row.earliestBarAt) }}</template></el-table-column>
          <el-table-column label="最新行情" min-width="185"><template #default="{row}">{{ formatTime(row.latestBarAt) }}</template></el-table-column>
          <el-table-column label="采集更新" min-width="185"><template #default="{row}">{{ formatTime(row.lastUpdatedAt) }}</template></el-table-column>
        </el-table>
      </section>
    </template>
  </section>
</template>
<style scoped>
.source-dashboard { min-width: 0; }
.source-metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
.source-metrics article { display:flex; flex-direction:column; gap:12px; }
.source-metrics strong { font-size:1.8rem; font-variant-numeric:tabular-nums; }
.source-metrics .date { font-size:1.1rem; }
.source-panels { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.source-panels > section { max-height:520px; overflow:auto; }
.inventory-bar { margin-block:18px; }
.inventory-bar > div { display:flex; justify-content:space-between; gap:12px; }
.inventory-bar meter { display:block; width:100%; height:18px; accent-color:var(--el-color-primary); }
.inventory-bar meter::-webkit-meter-bar { background:var(--el-fill-color-dark); border:0; }
.inventory-bar meter::-webkit-meter-optimum-value { background:var(--el-color-primary); }
.inventory-bar meter::-moz-meter-bar { background:var(--el-color-primary); }
.source-dashboard small, .source-dashboard summary { color:var(--el-text-color-secondary); }
.source-cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
.source-cards article { border:1px solid var(--el-border-color); border-radius:8px; padding:16px; background:var(--el-fill-color-light); }
.source-cards h3 { margin-top:0; }
@media (max-width:1000px) { .source-panels { grid-template-columns:1fr; } }
</style>
