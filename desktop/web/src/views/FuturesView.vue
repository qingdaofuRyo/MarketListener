<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import FuturesContractChart from "../components/futures/FuturesContractChart.vue";
import FuturesLongShortHeat from "../components/futures/FuturesLongShortHeat.vue";
import FuturesStructureChart from "../components/futures/FuturesStructureChart.vue";
import { apiGet } from "../domain/api";
import type {
  FuturesContractListResponse,
  FuturesContractSeriesKind,
  FuturesContractSeriesResponse,
  FuturesMemberPositionResponse,
  FuturesStructureRange,
  FuturesStructureResponse,
  LongShortHeatResponse,
} from "../domain/futures";

const payload = ref<LongShortHeatResponse>();
const loading = ref(false);
const error = ref("");
const structurePayload = ref<FuturesStructureResponse>();
const structureLoading = ref(false);
const structureError = ref("");
const structureRange = ref<FuturesStructureRange>("1y");
const structureLevel = ref<"main" | "other">("main");
const memberStructurePayload = ref<FuturesStructureResponse>();
const memberStructureLoading = ref(false);
const memberStructureError = ref("");
const memberStructureRange = ref<FuturesStructureRange>("1y");
const memberStructureLevel = ref<"main" | "other">("main");
const memberStructureDirection = ref("gross");
const memberPositionPayload = ref<FuturesMemberPositionResponse>();
const memberPositionLoading = ref(false);
const memberPositionError = ref("");
const memberPositionExchange = ref("");
const memberPositionProduct = ref("");
const memberPositionContract = ref("");
const contractOptionsPayload = ref<FuturesContractListResponse>();
const contractOptionsLoading = ref(false);
const contractOptionsError = ref("");
const contractSeriesPayload = ref<FuturesContractSeriesResponse>();
const contractSeriesLoading = ref(false);
const contractSeriesError = ref("");
const contractExchange = ref("");
const contractProduct = ref("");
const contractCode = ref("");
const contractSeriesKind = ref<FuturesContractSeriesKind>("CONTRACT");
const memberPositionExchanges = computed(() => [...new Set(memberPositionPayload.value?.contracts.map(item => item.exchange) || [])]);
const memberPositionProducts = computed(() => [...new Set(
  (memberPositionPayload.value?.contracts || [])
    .filter(item => !memberPositionExchange.value || item.exchange === memberPositionExchange.value)
    .map(item => item.productCode),
)]);
const memberPositionContracts = computed(() => (memberPositionPayload.value?.contracts || []).filter(item =>
  (!memberPositionExchange.value || item.exchange === memberPositionExchange.value)
  && (!memberPositionProduct.value || item.productCode === memberPositionProduct.value),
));
const contractExchanges = computed(() => [...new Set(contractOptionsPayload.value?.items.map(item => item.exchange) || [])]);
const contractProducts = computed(() => [...new Set(
  (contractOptionsPayload.value?.items || [])
    .filter(item => !contractExchange.value || item.exchange === contractExchange.value)
    .map(item => item.productCode),
)]);
const contractCodes = computed(() => (contractOptionsPayload.value?.items || []).filter(item =>
  item.seriesKind === contractSeriesKind.value
  && (!contractExchange.value || item.exchange === contractExchange.value)
  && (!contractProduct.value || item.productCode === contractProduct.value),
));
const hasWeightedContract = computed(() => (contractOptionsPayload.value?.items || []).some(item =>
  item.seriesKind === "WEIGHTED"
  && item.exchange === contractExchange.value
  && item.productCode === contractProduct.value,
));

async function load(force = false): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await apiGet<LongShortHeatResponse>("/api/futures/heat", undefined, { force, ttlMs: 60_000 });
  } catch (reason) {
    payload.value = undefined;
    error.value = reason instanceof Error ? reason.message : "多空热度加载失败";
  } finally {
    loading.value = false;
  }
}

async function loadStructure(force = false): Promise<void> {
  structureLoading.value = true;
  structureError.value = "";
  try {
    const query = new URLSearchParams({ range: structureRange.value, level: structureLevel.value });
    structurePayload.value = await apiGet<FuturesStructureResponse>(
      `/api/futures/structures/product-open-interest?${query.toString()}`,
      undefined,
      { force, ttlMs: 60_000 },
    );
  } catch (reason) {
    structurePayload.value = undefined;
    structureError.value = reason instanceof Error ? reason.message : "品种持仓结构加载失败";
  } finally {
    structureLoading.value = false;
  }
}

async function loadMemberStructure(force = false): Promise<void> {
  memberStructureLoading.value = true;
  memberStructureError.value = "";
  try {
    const query = new URLSearchParams({
      range: memberStructureRange.value,
      level: memberStructureLevel.value,
      direction: memberStructureDirection.value,
    });
    memberStructurePayload.value = await apiGet<FuturesStructureResponse>(
      `/api/futures/structures/member-open-interest?${query.toString()}`,
      undefined,
      { force, ttlMs: 60_000 },
    );
  } catch (reason) {
    memberStructurePayload.value = undefined;
    memberStructureError.value = reason instanceof Error ? reason.message : "席位持仓结构加载失败";
  } finally {
    memberStructureLoading.value = false;
  }
}

async function loadMemberPositions(force = false): Promise<void> {
  memberPositionLoading.value = true;
  memberPositionError.value = "";
  try {
    const query = new URLSearchParams();
    if (memberPositionExchange.value) query.set("exchange", memberPositionExchange.value);
    if (memberPositionProduct.value) query.set("product_code", memberPositionProduct.value);
    if (memberPositionContract.value) query.set("contract_code", memberPositionContract.value);
    memberPositionPayload.value = await apiGet<FuturesMemberPositionResponse>(
      `/api/futures/member-positions${query.size ? `?${query.toString()}` : ""}`,
      undefined,
      { force, ttlMs: 60_000 },
    );
  } catch (reason) {
    memberPositionPayload.value = undefined;
    memberPositionError.value = reason instanceof Error ? reason.message : "合约席位分布加载失败";
  } finally {
    memberPositionLoading.value = false;
  }
}

async function loadContractOptions(force = false): Promise<void> {
  contractOptionsLoading.value = true;
  contractOptionsError.value = "";
  try {
    contractOptionsPayload.value = await apiGet<FuturesContractListResponse>(
      "/api/futures/contracts",
      undefined,
      { force, ttlMs: 60_000 },
    );
  } catch (reason) {
    contractOptionsPayload.value = undefined;
    contractOptionsError.value = reason instanceof Error ? reason.message : "商品合约目录加载失败";
  } finally {
    contractOptionsLoading.value = false;
  }
}

async function loadContractSeries(force = false): Promise<void> {
  if (!contractExchange.value || !contractProduct.value || (contractSeriesKind.value === "CONTRACT" && !contractCode.value)) {
    contractSeriesPayload.value = undefined;
    return;
  }
  contractSeriesLoading.value = true;
  contractSeriesError.value = "";
  try {
    const query = new URLSearchParams({
      exchange: contractExchange.value,
      product: contractProduct.value,
      series_kind: contractSeriesKind.value,
    });
    if (contractSeriesKind.value === "CONTRACT") query.set("contract", contractCode.value);
    contractSeriesPayload.value = await apiGet<FuturesContractSeriesResponse>(
      `/api/futures/contract-series?${query.toString()}`,
      undefined,
      { force, ttlMs: 60_000 },
    );
  } catch (reason) {
    contractSeriesPayload.value = undefined;
    contractSeriesError.value = reason instanceof Error ? reason.message : "商品合约序列加载失败";
  } finally {
    contractSeriesLoading.value = false;
  }
}

function refresh(): void {
  void Promise.all([load(true), loadStructure(true), loadMemberStructure(true), loadMemberPositions(true), loadContractOptions(true), loadContractSeries(true)]);
}

function formatPosition(value: number | null): string {
  return value === null || !Number.isFinite(value) ? "—" : value.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

watch([structureRange, structureLevel], () => void loadStructure());
watch([memberStructureRange, memberStructureLevel, memberStructureDirection], () => void loadMemberStructure());
watch(memberPositionExchange, () => {
  memberPositionProduct.value = "";
  memberPositionContract.value = "";
  void loadMemberPositions();
});
watch(memberPositionProduct, () => {
  memberPositionContract.value = "";
  void loadMemberPositions();
});
watch(memberPositionContract, () => void loadMemberPositions());
watch(contractExchange, () => {
  contractProduct.value = "";
  contractCode.value = "";
  void loadContractSeries();
});
watch(contractProduct, () => {
  contractCode.value = "";
  contractSeriesKind.value = "CONTRACT";
  void loadContractSeries();
});
watch(contractSeriesKind, () => {
  contractCode.value = "";
  void loadContractSeries();
});
watch(contractCode, () => void loadContractSeries());

onMounted(() => {
  void load();
  void loadStructure();
  void loadMemberStructure();
  void loadMemberPositions();
  void loadContractOptions();
});
</script>

<template>
  <main class="futures-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">国内期货数据</h1>
        <p class="page-note">中国商品期货的市场广度、资金方向与结构数据；默认排除金融期货。</p>
      </div>
      <el-button :loading="loading || structureLoading || memberStructureLoading" data-test="futures-refresh" @click="refresh">刷新</el-button>
    </header>

    <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />
    <section v-if="loading && !payload" class="panel loading-panel" aria-label="正在加载多空热度">
      <el-skeleton :rows="7" animated />
    </section>
    <FuturesLongShortHeat v-else-if="payload?.available" :payload="payload" />
    <section v-else-if="payload && !payload.available" class="panel empty-state" data-test="futures-heat-empty">
      <h2>多空热度暂无可用数据</h2>
      <p>系统不会用演示序列替代真实期货行情，请先完成数据覆盖与 Gold 派生计算。</p>
    </section>

    <section class="panel futures-structure" data-test="product-open-interest-structure">
      <header class="structure-header">
        <div>
          <h2>品种持仓分布</h2>
          <p>有效月份合约的交易所单边持仓量，按固定基准顺序堆叠；不含中金所金融期货。</p>
        </div>
        <div class="structure-controls">
          <el-radio-group v-model="structureRange" size="small" aria-label="品种持仓结构时间范围">
            <el-radio-button value="1y">1年</el-radio-button><el-radio-button value="3y">3年</el-radio-button><el-radio-button value="5y">5年</el-radio-button><el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <el-button v-if="structurePayload?.otherMembers.length" size="small" @click="structureLevel = structureLevel === 'main' ? 'other' : 'main'">{{ structureLevel === 'main' ? `查看其他（${structurePayload.otherMembers.length}）` : '返回主图' }}</el-button>
        </div>
      </header>
      <el-alert v-if="structureError" :title="structureError" type="warning" :closable="false" />
      <el-skeleton v-else-if="structureLoading && !structurePayload" :rows="6" animated />
      <FuturesStructureChart v-else-if="structurePayload?.available" :payload="structurePayload" />
      <div v-else-if="structurePayload" class="structure-empty">
        <strong>品种持仓结构暂无可用数据</strong>
        <span>{{ structurePayload.limitations[0] }}</span>
      </div>
      <footer v-if="structurePayload?.available" class="structure-footnote">
        <span>基准日：{{ structurePayload.baselineDay }}</span>
        <span>阈值：{{ ((structurePayload.threshold || 0) * 100).toFixed(1) }}%</span>
        <span>公式版本：{{ structurePayload.formulaVersion }}</span>
        <span v-if="structurePayload.unclassifiedMembers.length">未分类新品种：{{ structurePayload.unclassifiedMembers.length }}</span>
        <ul><li v-for="item in structurePayload.limitations" :key="item">{{ item }}</li></ul>
      </footer>
    </section>

    <section class="panel futures-structure" data-test="member-open-interest-structure">
      <header class="structure-header">
        <div>
          <h2>席位持仓分布</h2>
          <p>交易所实际公布的会员排名覆盖范围，按交易所隔离席位并固定堆叠基准；不是全市场会员完整持仓。</p>
        </div>
        <div class="structure-controls">
          <el-radio-group v-model="memberStructureDirection" size="small" aria-label="席位持仓结构方向">
            <el-radio-button value="gross">毛持仓</el-radio-button><el-radio-button value="long">多头</el-radio-button><el-radio-button value="short">空头</el-radio-button><el-radio-button value="net-long">多头净持仓</el-radio-button><el-radio-button value="net-short">空头净持仓</el-radio-button>
          </el-radio-group>
          <el-radio-group v-model="memberStructureRange" size="small" aria-label="席位持仓结构时间范围">
            <el-radio-button value="1y">1年</el-radio-button><el-radio-button value="3y">3年</el-radio-button><el-radio-button value="5y">5年</el-radio-button><el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <el-button v-if="memberStructurePayload?.otherMembers.length" size="small" @click="memberStructureLevel = memberStructureLevel === 'main' ? 'other' : 'main'">{{ memberStructureLevel === 'main' ? `查看其他（${memberStructurePayload.otherMembers.length}）` : '返回主图' }}</el-button>
        </div>
      </header>
      <el-alert v-if="memberStructureError" :title="memberStructureError" type="warning" :closable="false" />
      <el-skeleton v-else-if="memberStructureLoading && !memberStructurePayload" :rows="6" animated />
      <FuturesStructureChart
        v-else-if="memberStructurePayload?.available"
        :payload="memberStructurePayload"
        axis-name="已公布排名持仓（张）"
        aria-label="中国商品期货席位持仓固定顺序堆叠面积图"
      />
      <div v-else-if="memberStructurePayload" class="structure-empty">
        <strong>席位持仓结构暂无可用数据</strong>
        <span>{{ memberStructurePayload.limitations[0] }}</span>
      </div>
      <footer v-if="memberStructurePayload?.available" class="structure-footnote">
        <span>基准日：{{ memberStructurePayload.baselineDay }}</span>
        <span>阈值：{{ ((memberStructurePayload.threshold || 0) * 100).toFixed(1) }}%</span>
        <span>方向：{{ memberStructurePayload.direction }}</span>
        <span>公式版本：{{ memberStructurePayload.formulaVersion }}</span>
        <ul><li v-for="item in memberStructurePayload.limitations" :key="item">{{ item }}</li></ul>
      </footer>
    </section>

    <section class="panel product-contract-panel" data-test="futures-contract-series">
      <header class="structure-header">
        <div>
          <h2>商品合约</h2>
          <p>同一容器展示本地日线 K 线、持仓量、名义持仓规模和基差；各指标使用独立 Y 轴与固定图例。</p>
        </div>
        <div class="member-position-filters">
          <el-select v-model="contractExchange" :loading="contractOptionsLoading" clearable placeholder="交易所" aria-label="商品合约交易所筛选">
            <el-option v-for="item in contractExchanges" :key="item" :label="item" :value="item" />
          </el-select>
          <el-select v-model="contractProduct" clearable placeholder="品种" :disabled="!contractProducts.length" aria-label="商品合约品种筛选">
            <el-option v-for="item in contractProducts" :key="item" :label="item" :value="item" />
          </el-select>
          <el-radio-group v-model="contractSeriesKind" size="small" aria-label="商品合约序列类型">
            <el-radio-button value="CONTRACT">月份合约</el-radio-button><el-radio-button value="WEIGHTED" :disabled="!hasWeightedContract">加权合约</el-radio-button>
          </el-radio-group>
          <el-select v-if="contractSeriesKind === 'CONTRACT'" v-model="contractCode" clearable placeholder="月份合约" :disabled="!contractCodes.length" aria-label="商品合约月份筛选">
            <el-option v-for="item in contractCodes" :key="item.instrumentId" :label="`${item.contractCode} · ${item.lastTradingDay || '—'}`" :value="item.contractCode" />
          </el-select>
        </div>
      </header>
      <el-alert v-if="contractOptionsError || contractSeriesError" :title="contractOptionsError || contractSeriesError" type="warning" :closable="false" />
      <el-skeleton v-else-if="contractSeriesLoading && !contractSeriesPayload" :rows="6" animated />
      <template v-else-if="contractSeriesPayload?.available">
        <FuturesContractChart :payload="contractSeriesPayload" />
        <footer class="structure-footnote">
          <span>来源：{{ contractSeriesPayload.source || "—" }}</span>
          <span>最后数据：{{ contractSeriesPayload.updatedAt || "—" }}</span>
          <span>名义规模价格基准：{{ contractSeriesPayload.priceBasis || "尚未锁定" }}</span>
          <ul><li v-for="item in contractSeriesPayload.limitations" :key="item">{{ item }}</li></ul>
        </footer>
      </template>
      <div v-else class="structure-empty">
        <strong>{{ contractSeriesKind === "WEIGHTED" ? "暂无可追溯加权合约" : "请选择交易所、品种和月份合约" }}</strong>
        <span>页面不会临时合成加权序列，也不会为名义规模或基差填充模拟值。</span>
      </div>
    </section>

    <section class="panel member-position-panel" data-test="futures-member-positions">
      <header class="structure-header">
        <div>
          <h2>商品合约席位分布</h2>
          <p>选择具体月份合约查看交易所已公布的多头、空头与可验证净持仓；未上榜方向保持空值。</p>
        </div>
        <div class="member-position-filters">
          <el-select v-model="memberPositionExchange" clearable placeholder="交易所" aria-label="席位排名交易所筛选">
            <el-option v-for="item in memberPositionExchanges" :key="item" :label="item" :value="item" />
          </el-select>
          <el-select v-model="memberPositionProduct" clearable placeholder="品种" :disabled="!memberPositionProducts.length" aria-label="席位排名品种筛选">
            <el-option v-for="item in memberPositionProducts" :key="item" :label="item" :value="item" />
          </el-select>
          <el-select v-model="memberPositionContract" clearable placeholder="月份合约" :disabled="!memberPositionContracts.length" aria-label="席位排名合约筛选">
            <el-option v-for="item in memberPositionContracts" :key="`${item.exchange}.${item.contractCode}`" :label="`${item.exchange} · ${item.contractCode}`" :value="item.contractCode" />
          </el-select>
        </div>
      </header>
      <el-alert v-if="memberPositionError" :title="memberPositionError" type="warning" :closable="false" />
      <el-skeleton v-else-if="memberPositionLoading && !memberPositionPayload" :rows="5" animated />
      <template v-else-if="memberPositionPayload?.available">
        <div class="member-position-coverage">
          <span>交易日：{{ memberPositionPayload.tradingDay }}</span>
          <span>已公布方向排名：{{ memberPositionPayload.coverage.publishedDirectionRankCount.toLocaleString("zh-CN") }}</span>
          <span>覆盖交易所：{{ memberPositionPayload.coverage.exchanges.join("、") || "—" }}</span>
          <span v-if="memberPositionPayload.coverage.missingExchanges.length">缺失交易所：{{ memberPositionPayload.coverage.missingExchanges.join("、") }}</span>
        </div>
        <el-table v-if="memberPositionPayload.rows.length" :data="memberPositionPayload.rows" size="small" max-height="440" class="member-position-table">
          <el-table-column prop="exchange" label="交易所" width="74" />
          <el-table-column prop="contractCode" label="合约" width="88" />
          <el-table-column prop="memberName" label="席位" min-width="150" />
          <el-table-column label="多头（名次）" min-width="130"><template #default="scope">{{ formatPosition(scope.row.longPosition) }}（{{ scope.row.longRank ?? "—" }}）</template></el-table-column>
          <el-table-column label="空头（名次）" min-width="130"><template #default="scope">{{ formatPosition(scope.row.shortPosition) }}（{{ scope.row.shortRank ?? "—" }}）</template></el-table-column>
          <el-table-column label="多头净" min-width="94"><template #default="scope">{{ formatPosition(scope.row.netLongPosition) }}</template></el-table-column>
          <el-table-column label="空头净" min-width="94"><template #default="scope">{{ formatPosition(scope.row.netShortPosition) }}</template></el-table-column>
          <el-table-column label="来源" min-width="190"><template #default="scope">{{ scope.row.sources.join("；") }}</template></el-table-column>
        </el-table>
        <div v-else class="structure-empty"><strong>请选择月份合约</strong><span>系统不会默认传输全市场全部席位明细。</span></div>
        <ul class="member-position-limitations"><li v-for="item in memberPositionPayload.limitations" :key="item">{{ item }}</li></ul>
      </template>
      <div v-else-if="memberPositionPayload" class="structure-empty"><strong>商品合约席位分布暂无可用数据</strong><span>{{ memberPositionPayload.limitations[0] }}</span></div>
    </section>

    <footer v-if="payload" class="futures-footnote">
      <span v-if="payload.generatedAt">生成时间：{{ payload.generatedAt }}</span>
      <span v-if="payload.source">来源：{{ payload.source }}</span>
      <span v-if="payload.formulaVersion">公式版本：{{ payload.formulaVersion }}</span>
      <ul v-if="payload.limitations?.length">
        <li v-for="item in payload.limitations" :key="item">{{ item }}</li>
      </ul>
    </footer>
  </main>
</template>

<style scoped>
.futures-page { max-width: 1560px; margin: 0 auto; }
.loading-panel { min-height: 520px; }
.empty-state { padding-block: 54px; text-align: center; }
.empty-state p { margin: 8px 0 0; color: var(--ml-text-secondary); font-size: 13px; }
.futures-footnote {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  margin: 12px 4px 0;
  color: var(--ml-text-disabled);
  font-size: 11px;
}
.futures-footnote ul { flex-basis: 100%; margin: 0; padding-left: 18px; }
.futures-structure { margin-top: 16px; }
.structure-header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 12px; }
.structure-header h2 { margin: 0; font-size: 17px; }
.structure-header p { margin: 5px 0 0; color: var(--ml-text-secondary); font-size: 12px; }
.structure-controls { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.structure-empty { min-height: 180px; display: grid; place-content: center; gap: 8px; text-align: center; color: var(--ml-text-secondary); }
.structure-empty strong { color: var(--ml-text-primary); }
.structure-footnote { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 8px; color: var(--ml-text-disabled); font-size: 11px; }
.structure-footnote ul { flex-basis: 100%; margin: 0; padding-left: 18px; }
.member-position-panel { margin-top: 16px; }
.product-contract-panel { margin-top: 16px; }
.member-position-filters { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.member-position-filters .el-select { width: 146px; }
.member-position-coverage { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-bottom: 8px; color: var(--ml-text-secondary); font-size: 12px; }
.member-position-limitations { margin: 8px 0 0; padding-left: 18px; color: var(--ml-text-disabled); font-size: 11px; }
@media (max-width: 720px) { .member-position-filters { justify-content: flex-start; } .member-position-filters .el-select { width: min(100%, 240px); } }
@media (max-width: 720px) { .structure-header { display: grid; } .structure-controls { justify-content: flex-start; } }
</style>
