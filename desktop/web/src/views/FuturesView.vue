<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import FuturesContractTurnoverHeat from "../components/futures/FuturesContractTurnoverHeat.vue";
import FuturesPositionPieChart from "../components/futures/FuturesPositionPieChart.vue";
import FuturesPositionTrendChart from "../components/futures/FuturesPositionTrendChart.vue";
import FuturesStructureChart from "../components/futures/FuturesStructureChart.vue";
import FuturesStructureLineChart from "../components/futures/FuturesStructureLineChart.vue";
import { apiGet } from "../domain/api";
import { commodityMemberCharts, memberCommodityCharts, type PositionMetric } from "../domain/futuresMemberCharts";
import type {
  FuturesMemberPositionHistoryResponse,
  FuturesMemberOverviewResponse,
  FuturesMemberPositionResponse,
  ContractTurnoverHeatResponse,
  FuturesStructureRange,
  FuturesStructureResponse,
} from "../domain/futures";

const payload = ref<ContractTurnoverHeatResponse>();
const loading = ref(false);
const error = ref("");
const structurePayload = ref<FuturesStructureResponse>();
const structureLoading = ref(false);
const structureError = ref("");
const structureRange = ref<FuturesStructureRange>("1y");
const structureLevel = ref<"main" | "other">("main");
const structureSource = ref<"official" | "tdx">("official");
const notionalStructurePayload = ref<FuturesStructureResponse>();
const notionalStructureLoading = ref(false);
const notionalStructureError = ref("");
const notionalStructureRange = ref<FuturesStructureRange>("1y");
const notionalStructureLevel = ref<"main" | "other">("main");
const notionalStructureSource = ref<"official" | "tdx">("official");
const volumeStructurePayload = ref<FuturesStructureResponse>();
const volumeStructureLoading = ref(false);
const volumeStructureError = ref("");
const volumeStructureRange = ref<FuturesStructureRange>("1y");
const volumeStructureLevel = ref<"main" | "other">("main");
const memberPositionPayload = ref<FuturesMemberPositionResponse>();
const memberPositionLoading = ref(false);
const memberPositionError = ref("");
const overviewPayload = ref<FuturesMemberOverviewResponse>();
const overviewLoading = ref(false);
const overviewError = ref("");
const rankingDay = ref("");
const memberPositionProduct = ref("");
const memberPositionContract = ref("");
const commodityRankingPayload = ref<FuturesMemberPositionResponse>();
const commodityRankingLoading = ref(false);
const commodityRankingError = ref("");
const memberInventoryKey = ref("");
const memberHistoryPayload = ref<FuturesMemberPositionHistoryResponse>();
const memberHistoryLoading = ref(false);
const memberHistoryError = ref("");
const commodityHistoryPayload = ref<FuturesMemberPositionHistoryResponse>();
const commodityHistoryLoading = ref(false);
const commodityHistoryError = ref("");
let rankingDirectoryRevision = 0;
let overviewRevision = 0;
let commodityRankingRevision = 0;
let memberHistoryRevision = 0;
let commodityHistoryRevision = 0;
const commodityMetrics: Array<{ key: Exclude<PositionMetric, "gross">; title: string }> = [
  { key: "longPosition", title: "多头持仓" },
  { key: "shortPosition", title: "空头持仓" },
  { key: "netLongPosition", title: "净多持仓" },
  { key: "netShortPosition", title: "净空持仓" },
];
const memberPositionProducts = computed(() => overviewPayload.value?.products || []);
const selectedProduct = computed(() => memberPositionProducts.value.find(item => item.productKey === memberPositionProduct.value));
const memberPositionContracts = computed(() => selectedProduct.value?.contracts || []);
const memberInventoryMembers = computed(() => overviewPayload.value?.members || []);
const selectedRankingDay = computed(() => rankingDay.value || overviewPayload.value?.tradingDay || "");
const memberDetailRows = computed(() => (memberHistoryPayload.value?.rows || [])
  .filter(item => item.tradingDay === selectedRankingDay.value));
const memberCharts = computed(() => memberHistoryPayload.value
  ? memberCommodityCharts(memberHistoryPayload.value, selectedRankingDay.value) : null);
const commodityCharts = computed(() => commodityHistoryPayload.value
  ? commodityMetrics.map(metric => ({
    ...metric,
    ...commodityMemberCharts(commodityHistoryPayload.value!, selectedRankingDay.value, metric.key),
  })) : []);
const notionalLatestCoverage = computed(() => {
  const rows = notionalStructurePayload.value?.coverage || [];
  return rows[rows.length - 1];
});
const notionalPartialDays = computed(() =>
  (notionalStructurePayload.value?.coverage || []).filter(item => item.dataQualityStatus === "PARTIAL").length,
);
const notionalLatestOfficialMissing = computed(() => {
  const row = notionalLatestCoverage.value;
  if (!row) return 0;
  return (row.missingSettlementCount || 0)
    + (row.missingOpenInterestCount || 0)
    + (row.missingMultiplierCount || 0)
    + (row.missingExchangeCount || 0)
    + (row.unverifiedOpenInterestCount || 0);
});
const oiLatestCoverage = computed(() => structurePayload.value?.coverage.at(-1));
const volumeLatestCoverage = computed(() => volumeStructurePayload.value?.coverage.at(-1));
const backendNeedsRestart = computed(() => (
  (memberPositionPayload.value !== undefined && memberPositionPayload.value.latestPublishedDay === undefined)
  || overviewError.value === "Not Found"
  || (structurePayload.value?.available && structurePayload.value.formulaVersion !== (structureSource.value === "official" ? "futures-structure-oi-official-v1" : "futures-structure-oi-top40-v3"))
  || (payload.value !== undefined && payload.value.formulaVersion !== "2026-09-contract-turnover-v2")
  || error.value === "Not Found"
  || error.value === "请求失败 (404)"
));
const memberProductRows = computed(() => {
  const grouped = new Map<string, {
    exchange: string; productCode: string; productDisplayName: string; contracts: string[];
    longPosition: number | null; shortPosition: number | null;
  }>();
  for (const row of memberDetailRows.value) {
    const key = row.productKey;
    const item = grouped.get(key) || {
      exchange: row.exchange, productCode: row.productCode,
      productDisplayName: row.productDisplayName, contracts: [],
      longPosition: null, shortPosition: null,
    };
    item.contracts.push(row.contractCode);
    if (row.longPosition !== null) item.longPosition = (item.longPosition ?? 0) + row.longPosition;
    if (row.shortPosition !== null) item.shortPosition = (item.shortPosition ?? 0) + row.shortPosition;
    grouped.set(key, item);
  }
  return [...grouped.values()]
    .map(item => ({ ...item, contracts: [...new Set(item.contracts)].sort() }))
    .sort((left, right) => left.exchange.localeCompare(right.exchange) || left.productCode.localeCompare(right.productCode));
});

async function load(force = true): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await apiGet<ContractTurnoverHeatResponse>("/api/futures/contract-heat", undefined, { force, ttlMs: 60_000 });
  } catch (reason) {
    payload.value = undefined;
    error.value = reason instanceof Error ? reason.message : "月份合约多空热度加载失败";
  } finally {
    loading.value = false;
  }
}

async function loadStructure(force = true): Promise<void> {
  structureLoading.value = true;
  structureError.value = "";
  try {
    const query = new URLSearchParams({ range: structureRange.value, level: structureLevel.value, source: structureSource.value });
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

async function loadNotionalStructure(force = true): Promise<void> {
  notionalStructureLoading.value = true;
  notionalStructureError.value = "";
  try {
    const query = new URLSearchParams({ range: notionalStructureRange.value, level: notionalStructureLevel.value, source: notionalStructureSource.value });
    notionalStructurePayload.value = await apiGet<FuturesStructureResponse>(
      `/api/futures/structures/product-notional?${query.toString()}`,
      undefined,
      { force, ttlMs: 60_000 },
    );
  } catch (reason) {
    notionalStructurePayload.value = undefined;
    notionalStructureError.value = reason instanceof Error ? reason.message : "品种市值结构加载失败";
  } finally {
    notionalStructureLoading.value = false;
  }
}

async function loadVolumeStructure(force = true): Promise<void> {
  volumeStructureLoading.value = true;
  volumeStructureError.value = "";
  try {
    const query = new URLSearchParams({ range: volumeStructureRange.value, level: volumeStructureLevel.value, source: "official" });
    volumeStructurePayload.value = await apiGet<FuturesStructureResponse>(
      `/api/futures/structures/product-volume?${query.toString()}`,
      undefined,
      { force, ttlMs: 60_000 },
    );
  } catch (reason) {
    volumeStructurePayload.value = undefined;
    volumeStructureError.value = reason instanceof Error ? reason.message : "品种成交量结构加载失败";
  } finally {
    volumeStructureLoading.value = false;
  }
}

async function loadOverview(force = true): Promise<void> {
  const revision = ++overviewRevision;
  overviewLoading.value = true;
  overviewError.value = "";
  try {
    const params = new URLSearchParams({ scope: "all" });
    if (rankingDay.value) params.set("trading_day", rankingDay.value);
    const response = await apiGet<FuturesMemberOverviewResponse>(
      `/api/futures/member-positions/overview?${params.toString()}`, undefined, { force, ttlMs: 60_000 },
    );
    if (revision !== overviewRevision) return;
    overviewPayload.value = response;
    if (!response.members.some(item => item.memberKey === memberInventoryKey.value)) {
      memberInventoryKey.value = response.defaultMemberKey || "";
    }
    if (!response.products.some(item => item.productKey === memberPositionProduct.value)) {
      memberPositionProduct.value = response.defaultProductKey || "";
    }
  } catch (reason) {
    if (revision === overviewRevision) {
      overviewPayload.value = undefined;
      overviewError.value = reason instanceof Error ? reason.message : "席位概览加载失败";
    }
  } finally {
    if (revision === overviewRevision) overviewLoading.value = false;
  }
}

async function loadMemberPositions(force = true): Promise<void> {
  const revision = ++rankingDirectoryRevision;
  memberPositionLoading.value = true;
  memberPositionError.value = "";
  try {
    const query = new URLSearchParams();
    query.set("scope", "all");
    if (selectedRankingDay.value) query.set("trading_day", selectedRankingDay.value);
    const response = await apiGet<FuturesMemberPositionResponse>(
      `/api/futures/member-positions${query.size ? `?${query.toString()}` : ""}`,
      undefined,
      { force, ttlMs: 60_000 },
    );
    if (revision === rankingDirectoryRevision) memberPositionPayload.value = response;
  } catch (reason) {
    if (revision === rankingDirectoryRevision) {
      memberPositionPayload.value = undefined;
      memberPositionError.value = reason instanceof Error ? reason.message : "龙虎榜目录加载失败";
    }
  } finally {
    if (revision === rankingDirectoryRevision) memberPositionLoading.value = false;
  }
}

async function loadCommodityRanking(force = false): Promise<void> {
  const revision = ++commodityRankingRevision;
  if (!selectedProduct.value) {
    commodityRankingPayload.value = undefined;
    commodityRankingLoading.value = false;
    return;
  }
  commodityRankingLoading.value = true;
  commodityRankingError.value = "";
  try {
    const query = new URLSearchParams({
      product_key: selectedProduct.value.productKey,
      scope: "all",
    });
    if (selectedRankingDay.value) query.set("trading_day", selectedRankingDay.value);
    if (memberPositionContract.value) query.set("contract_code", memberPositionContract.value);
    const response = await apiGet<FuturesMemberPositionResponse>(
      `/api/futures/member-positions?${query.toString()}`, undefined, { force, ttlMs: 60_000 },
    );
    if (revision === commodityRankingRevision) commodityRankingPayload.value = response;
  } catch (reason) {
    if (revision === commodityRankingRevision) {
      commodityRankingPayload.value = undefined;
      commodityRankingError.value = reason instanceof Error ? reason.message : "商品席位龙虎榜加载失败";
    }
  } finally {
    if (revision === commodityRankingRevision) commodityRankingLoading.value = false;
  }
}

async function loadMemberHistory(force = true): Promise<void> {
  const revision = ++memberHistoryRevision;
  if (!memberInventoryKey.value) {
    memberHistoryPayload.value = undefined;
    memberHistoryLoading.value = false;
    return;
  }
  memberHistoryLoading.value = true;
  memberHistoryError.value = "";
  try {
    const query = new URLSearchParams({ member_key: memberInventoryKey.value, scope: "all" });
    const response = await apiGet<FuturesMemberPositionHistoryResponse>(
      `/api/futures/member-positions/history?${query.toString()}`,
      undefined, { force, ttlMs: 60_000 },
    );
    if (revision === memberHistoryRevision) memberHistoryPayload.value = response;
  } catch (reason) {
    if (revision === memberHistoryRevision) {
      memberHistoryPayload.value = undefined;
      memberHistoryError.value = reason instanceof Error ? reason.message : "席位历史持仓加载失败";
    }
  } finally {
    if (revision === memberHistoryRevision) memberHistoryLoading.value = false;
  }
}

async function loadCommodityHistory(force = true): Promise<void> {
  const revision = ++commodityHistoryRevision;
  if (!memberPositionProduct.value) {
    commodityHistoryPayload.value = undefined;
    commodityHistoryLoading.value = false;
    return;
  }
  commodityHistoryLoading.value = true;
  commodityHistoryError.value = "";
  try {
    const query = new URLSearchParams({ product_key: memberPositionProduct.value, scope: "all" });
    if (memberPositionContract.value) query.set("contract_code", memberPositionContract.value);
    const response = await apiGet<FuturesMemberPositionHistoryResponse>(
      `/api/futures/member-positions/history?${query.toString()}`,
      undefined, { force, ttlMs: 60_000 },
    );
    if (revision === commodityHistoryRevision) commodityHistoryPayload.value = response;
  } catch (reason) {
    if (revision === commodityHistoryRevision) {
      commodityHistoryPayload.value = undefined;
      commodityHistoryError.value = reason instanceof Error ? reason.message : "商品席位历史加载失败";
    }
  } finally {
    if (revision === commodityHistoryRevision) commodityHistoryLoading.value = false;
  }
}

function refresh(): void {
  void Promise.all([load(true), loadStructure(true), loadNotionalStructure(true), loadVolumeStructure(true), loadOverview(true)])
    .then(() => Promise.all([
      loadMemberPositions(true), loadCommodityRanking(true),
      loadCommodityHistory(true), loadMemberHistory(true),
    ]));
}

function formatPosition(value: number | null): string {
  return value === null || !Number.isFinite(value) ? "—" : value.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function formatSourceHashes(values?: string[]): string {
  return values?.map(hash => hash.slice(0, 12)).join("、") || "—";
}

function cffexMemberSummary(value: FuturesStructureResponse): string {
  const primary = value.primaryMembers.filter(item => item.memberKey.startsWith("CFFEX.")).length;
  const other = value.otherMembers.filter(item => item.memberKey.startsWith("CFFEX.")).length;
  return `中金所品种 ${primary + other} 个（主图 ${primary}、其他 ${other}）`;
}

watch([structureRange, structureLevel, structureSource], () => void loadStructure());
watch([notionalStructureRange, notionalStructureLevel, notionalStructureSource], () => void loadNotionalStructure());
watch([volumeStructureRange, volumeStructureLevel], () => void loadVolumeStructure());
watch(rankingDay, () => {
  memberPositionPayload.value = undefined;
  commodityRankingPayload.value = undefined;
  void loadOverview().then(() => {
    void loadMemberPositions();
    void loadCommodityRanking();
  });
});
watch(memberPositionProduct, () => {
  memberPositionContract.value = "";
  void loadCommodityRanking();
  void loadCommodityHistory();
});
watch(memberPositionContract, () => {
  void loadCommodityRanking();
  void loadCommodityHistory();
});
watch(memberInventoryKey, () => void loadMemberHistory());

onMounted(() => {
  void load();
  void loadStructure();
  void loadNotionalStructure();
  void loadVolumeStructure();
  void loadOverview().then(() => void loadMemberPositions());
});
</script>

<template>
  <main class="futures-page">
    <header class="page-heading">
      <div>
        <h1 class="page-title">国内期货数据</h1>
        <p class="page-note">多空热度按商品期货统计；品种成交、持仓和名义规模覆盖国内期货，两个席位视角纳入商品期货和中金所金融期货。</p>
      </div>
      <div class="member-position-filters">
        <el-date-picker v-model="rankingDay" type="date" value-format="YYYY-MM-DD" clearable placeholder="龙虎榜交易日（默认最新）" aria-label="龙虎榜交易日" />
        <el-button :loading="loading || structureLoading || notionalStructureLoading || volumeStructureLoading || overviewLoading || memberPositionLoading || commodityRankingLoading || memberHistoryLoading || commodityHistoryLoading" data-test="futures-refresh" @click="refresh">刷新</el-button>
      </div>
    </header>

    <el-alert v-if="backendNeedsRestart" title="本机服务仍在运行旧版后端；当前图表和龙虎榜接口尚未加载本次更新，请重启 8765 服务后刷新页面。" type="warning" :closable="false" class="page-alert" />
    <el-alert v-if="overviewPayload?.latestObservationDay && overviewPayload.latestObservationDay !== overviewPayload.tradingDay" :title="`最近采集日 ${overviewPayload.latestObservationDay} 未取得新的完整排名；席位图默认显示最近有实际排名的 ${overviewPayload.tradingDay}。缺失交易所：${overviewPayload.latestObservationCoverage?.missingExchanges.join('、') || '无'}。`" type="info" :closable="false" class="page-alert" />
    <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />
    <section v-if="loading && !payload" class="panel loading-panel" aria-label="正在加载商品月份合约多空热度">
      <el-skeleton :rows="7" animated />
    </section>
    <FuturesContractTurnoverHeat v-else-if="payload?.available" :payload="payload" />
    <section v-else-if="payload && !payload.available" class="panel empty-state" data-test="futures-heat-empty">
      <h2>商品月份合约多空热度暂无可用数据</h2>
      <p>系统不会用演示序列替代真实期货行情，请先更新本地月份合约、同步交易所官方成交额并完成派生计算。</p>
    </section>

    <section class="panel futures-structure" data-test="product-open-interest-structure">
      <header class="structure-header">
        <div>
          <h2>品种持仓分布</h2>
          <p>国内期货（含中金所）有效月份合约的交易所单边持仓量；按最新完整基准日从多到少固定展示前40个品种，其余列入“其他”。</p>
        </div>
        <div class="structure-controls">
          <el-radio-group v-model="structureSource" size="small" aria-label="品种持仓数据来源">
            <el-radio-button value="official">交易所官网</el-radio-button><el-radio-button value="tdx">通达信</el-radio-button>
          </el-radio-group>
          <el-radio-group v-model="structureRange" size="small" aria-label="品种持仓结构时间范围">
            <el-radio-button value="1y">1年</el-radio-button><el-radio-button value="3y">3年</el-radio-button><el-radio-button value="5y">5年</el-radio-button><el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <el-button v-if="structurePayload?.otherMembers.length" size="small" @click="structureLevel = structureLevel === 'main' ? 'other' : 'main'">{{ structureLevel === 'main' ? `查看其他（${structurePayload.otherMembers.length}）` : '返回主图' }}</el-button>
        </div>
      </header>
      <el-alert v-if="structureError" :title="structureError" type="warning" :closable="false" />
      <el-alert v-if="structureSource === 'official' && oiLatestCoverage?.dataQualityStatus === 'PARTIAL'" :title="`截至 ${oiLatestCoverage.tradeDate}，官方行情缺少 ${oiLatestCoverage.missingExchangeCount || 0} 个交易所覆盖；面积总量留空，已核实品种折线仍保留。`" type="info" :closable="false" class="structure-coverage-warning" />
      <el-skeleton v-if="structureLoading && !structurePayload" :rows="6" animated />
      <template v-else-if="structurePayload?.available">
        <FuturesStructureChart :payload="structurePayload" />
        <FuturesStructureLineChart :payload="structurePayload" axis-name="单边持仓（张）" aria-label="国内期货品种单边持仓日度折线图" />
      </template>
      <div v-else-if="structurePayload" class="structure-empty">
        <strong>品种持仓结构暂无可用数据</strong>
        <span>{{ structurePayload.limitations[0] }}</span>
      </div>
      <footer v-if="structurePayload?.available" class="structure-footnote">
        <span>基准日：{{ structurePayload.baselineDay }}</span>
        <span v-if="structureSource === 'official'">基准质量：{{ structurePayload.baselineQualityStatus === 'PASS' ? '完整' : structurePayload.baselineQualityStatus === 'PARTIAL' ? '部分' : '未知' }}</span>
        <span>基准日前 {{ structurePayload.rankLimit ?? 40 }} 名</span>
        <span>{{ cffexMemberSummary(structurePayload) }}</span>
        <span>公式版本：{{ structurePayload.formulaVersion }}</span>
        <span v-if="structurePayload.unclassifiedMembers.length">未分类新品种：{{ structurePayload.unclassifiedMembers.length }}</span>
        <ul><li v-for="item in structurePayload.limitations" :key="item">{{ item }}</li></ul>
      </footer>
    </section>

    <section class="panel futures-structure" data-test="product-notional-structure">
      <header class="structure-header">
        <div>
          <h2>品种市值分布</h2>
          <p>国内期货（含中金所）有效月份合约按“结算价 × 交易乘数 × 交易所单边持仓量”汇总；按最新完整基准日名义规模展示前40个品种，其余列入“其他”。名义规模使用交易所官方有效期乘数，不要求每天重复发布同一规则快照；缺少官方乘数、结算价或持仓量时留空。</p>
        </div>
        <div class="structure-controls">
          <el-radio-group v-model="notionalStructureSource" size="small" aria-label="品种市值数据来源">
            <el-radio-button value="official">交易所官网</el-radio-button><el-radio-button value="tdx">通达信</el-radio-button>
          </el-radio-group>
          <el-radio-group v-model="notionalStructureRange" size="small" aria-label="品种市值结构时间范围">
            <el-radio-button value="1y">1年</el-radio-button><el-radio-button value="3y">3年</el-radio-button><el-radio-button value="5y">5年</el-radio-button><el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <el-button v-if="notionalStructurePayload?.otherMembers.length" size="small" @click="notionalStructureLevel = notionalStructureLevel === 'main' ? 'other' : 'main'">{{ notionalStructureLevel === 'main' ? `查看其他（${notionalStructurePayload.otherMembers.length}）` : '返回主图' }}</el-button>
        </div>
      </header>
      <el-alert v-if="notionalStructureError" :title="notionalStructureError" type="warning" :closable="false" />
      <el-alert v-if="notionalLatestCoverage?.dataQualityStatus === 'PARTIAL'" :title="`截至 ${notionalLatestCoverage.tradeDate}，官方有效输入 ${notionalLatestCoverage.inputRowCount - notionalLatestCoverage.missingRowCount}/${notionalLatestCoverage.inputRowCount} 条；缺失或未核验的输入不参与该日市场总规模。`" type="warning" :closable="false" class="structure-coverage-warning" />
      <div v-if="notionalLatestCoverage && notionalLatestOfficialMissing" class="structure-coverage-detail" data-test="notional-gap-details">
        <span>官方来源诊断：交易所覆盖缺 {{ notionalLatestCoverage.missingExchangeCount || 0 }} 个、结算价缺 {{ notionalLatestCoverage.missingSettlementCount || 0 }} 条、单边持仓缺/未核验 {{ (notionalLatestCoverage.missingOpenInterestCount || 0) + (notionalLatestCoverage.unverifiedOpenInterestCount || 0) }} 条、官方乘数缺 {{ notionalLatestCoverage.missingMultiplierCount || 0 }} 条。</span>
        <span v-if="notionalLatestCoverage.missingExchanges?.length">缺少交易所：{{ notionalLatestCoverage.missingExchanges.join("、") }}</span>
        <span v-if="notionalLatestCoverage.missingProducts?.length">受影响品种：{{ notionalLatestCoverage.missingProducts.join("、") }}</span>
        <span v-if="notionalLatestCoverage.missingContracts?.length">受影响合约：{{ notionalLatestCoverage.missingContracts.slice(0, 8).join("、") }}{{ notionalLatestCoverage.missingContracts.length > 8 ? `等 ${notionalLatestCoverage.missingContracts.length} 个` : "" }}</span>
      </div>
      <el-alert v-else-if="notionalPartialDays" :title="`所选范围有 ${notionalPartialDays} 个交易日缺少合约结算价、乘数或持仓量，图表在这些日期留空，避免部分合计造成虚假市值跳变。`" type="info" :closable="false" class="structure-coverage-warning" />
      <el-skeleton v-if="notionalStructureLoading && !notionalStructurePayload" :rows="6" animated />
      <template v-else-if="notionalStructurePayload?.available">
        <FuturesStructureChart :payload="notionalStructurePayload" axis-name="名义持仓规模（元）" aria-label="国内期货品种市值固定顺序堆叠面积图" total-label="完整日合约名义规模" />
        <FuturesStructureLineChart :payload="notionalStructurePayload" axis-name="名义持仓规模（元）" aria-label="国内期货品种名义持仓规模日度折线图" />
      </template>
      <div v-else-if="notionalStructurePayload" class="structure-empty">
        <strong>品种市值结构暂无可用数据</strong>
        <span>{{ notionalStructurePayload.limitations[0] }}</span>
      </div>
      <footer v-if="notionalStructurePayload?.available" class="structure-footnote">
        <span>基准日：{{ notionalStructurePayload.baselineDay }}</span>
        <span v-if="notionalStructureSource === 'official'">基准质量：{{ notionalStructurePayload.baselineQualityStatus === 'PASS' ? '完整' : notionalStructurePayload.baselineQualityStatus === 'PARTIAL' ? '部分' : '未知' }}</span>
        <span>价格基准：结算价</span>
        <span>基准日前 {{ notionalStructurePayload.rankLimit ?? 40 }} 名</span>
        <span>{{ cffexMemberSummary(notionalStructurePayload) }}</span>
        <span>公式版本：{{ notionalStructurePayload.formulaVersion }}</span>
        <span v-if="notionalStructurePayload.unclassifiedMembers.length">未纳入基准新品种：{{ notionalStructurePayload.unclassifiedMembers.length }}</span>
        <ul><li v-for="item in notionalStructurePayload.limitations" :key="item">{{ item }}</li></ul>
      </footer>
    </section>

    <section class="panel futures-structure" data-test="product-volume-structure">
      <header class="structure-header">
        <div>
          <h2>品种成交分布</h2>
          <p>按交易所官方逐合约日行情累计成交量（手），仅计入单边口径已核验的来源；成交排名另行留档，本图展示全市场合约成交量。</p>
        </div>
        <div class="structure-controls">
          <el-radio-group v-model="volumeStructureRange" size="small" aria-label="品种成交结构时间范围">
            <el-radio-button value="1y">1年</el-radio-button><el-radio-button value="3y">3年</el-radio-button><el-radio-button value="5y">5年</el-radio-button><el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <el-button v-if="volumeStructurePayload?.otherMembers.length" size="small" @click="volumeStructureLevel = volumeStructureLevel === 'main' ? 'other' : 'main'">{{ volumeStructureLevel === 'main' ? `查看其他（${volumeStructurePayload.otherMembers.length}）` : '返回主图' }}</el-button>
        </div>
      </header>
      <el-alert v-if="volumeStructureError" :title="volumeStructureError" type="warning" :closable="false" />
      <el-alert v-if="volumeLatestCoverage?.dataQualityStatus === 'PARTIAL'" :title="`截至 ${volumeLatestCoverage.tradeDate}，部分成交量或交易所口径未核验；面积总量留空，已核实品种折线仍保留。`" type="info" :closable="false" class="structure-coverage-warning" />
      <el-skeleton v-if="volumeStructureLoading && !volumeStructurePayload" :rows="6" animated />
      <template v-else-if="volumeStructurePayload?.available">
        <FuturesStructureChart :payload="volumeStructurePayload" axis-name="成交量（手）" aria-label="国内期货品种成交量固定顺序堆叠面积图" total-label="完整日成交量" />
        <FuturesStructureLineChart :payload="volumeStructurePayload" axis-name="成交量（手）" aria-label="国内期货品种成交量日度折线图" />
      </template>
      <div v-else-if="volumeStructurePayload" class="structure-empty">
        <strong>品种成交分布暂无可用数据</strong>
        <span>{{ volumeStructurePayload.limitations[0] }}</span>
      </div>
      <footer v-if="volumeStructurePayload?.available" class="structure-footnote">
        <span>基准日：{{ volumeStructurePayload.baselineDay }}</span>
        <span>基准质量：{{ volumeStructurePayload.baselineQualityStatus === 'PASS' ? '完整' : volumeStructurePayload.baselineQualityStatus === 'PARTIAL' ? '部分' : '未知' }}</span>
        <span>基准日前 {{ volumeStructurePayload.rankLimit ?? 40 }} 名</span>
        <span>公式版本：{{ volumeStructurePayload.formulaVersion }}</span>
        <ul><li v-for="item in volumeStructurePayload.limitations" :key="item">{{ item }}</li></ul>
      </footer>
    </section>

    <section class="panel member-position-panel" data-test="member-product-positions">
      <header class="structure-header">
        <div>
          <h2>席位持仓分布</h2>
          <p>默认显示最近公布日持仓最大的席位。饼图展示已公布品种持仓，历史面积图及正负柱展示持仓和当日增减。</p>
        </div>
        <div class="member-position-filters">
          <el-select v-model="memberInventoryKey" filterable clearable placeholder="席位公司" :disabled="!memberInventoryMembers.length" aria-label="席位公司筛选">
            <el-option v-for="item in memberInventoryMembers" :key="item.memberKey" :label="`${item.memberName} · ${formatPosition(item.grossPublishedPosition)}张`" :value="item.memberKey" />
          </el-select>
        </div>
      </header>
      <el-alert v-if="overviewError" :title="overviewError" type="warning" :closable="false" />
      <el-alert v-if="memberHistoryError" :title="memberHistoryError" type="warning" :closable="false" />
      <el-skeleton v-else-if="overviewLoading && !overviewPayload" :rows="5" animated />
      <div class="member-position-coverage">交易日：{{ selectedRankingDay || "—" }} · 当前排行仅计入已公布的多头＋空头持仓</div>
      <div v-if="overviewPayload?.latestObservationDay && overviewPayload.latestObservationDay !== selectedRankingDay" class="member-position-coverage" data-test="member-latest-observation">
        <span>最近采集观察日：{{ overviewPayload.latestObservationDay }}</span>
        <span v-if="overviewPayload.latestObservationCoverage?.missingExchanges.length">该次采集未覆盖：{{ overviewPayload.latestObservationCoverage.missingExchanges.join("、") }}</span>
        <span v-else>该次采集覆盖状态：{{ overviewPayload.latestObservationCoverage?.dataQualityStatus || "待核验" }}</span>
      </div>
      <el-button v-if="overviewPayload?.latestPublishedDay && !overviewPayload.available && overviewPayload.latestPublishedDay !== overviewPayload.tradingDay" size="small" @click="rankingDay = overviewPayload.latestPublishedDay">查看最近有排名的 {{ overviewPayload.latestPublishedDay }}</el-button>
      <el-skeleton v-if="memberHistoryLoading && !memberHistoryPayload" :rows="6" animated />
      <div v-else-if="memberHistoryPayload?.available && memberCharts" class="position-chart-grid" data-test="member-product-charts">
        <FuturesPositionPieChart :title="`${selectedRankingDay} 已公布商品持仓占比`" :slices="memberCharts.pie" />
        <FuturesPositionTrendChart title="各商品已公布持仓与增减历史" :dates="memberHistoryPayload.dates" :series="memberCharts.trend" :change-totals="memberCharts.changeTotals" area />
      </div>
      <div v-else-if="memberInventoryKey && !memberHistoryLoading" class="structure-empty"><strong>该席位暂无历史排名</strong></div>
      <details v-if="memberProductRows.length" class="position-details">
        <summary>查看当日已公布商品与合约明细</summary>
        <el-table :data="memberProductRows" size="small" max-height="330" data-test="member-products-table">
        <el-table-column prop="exchange" label="交易所" width="82" />
        <el-table-column prop="productDisplayName" label="持仓品种" min-width="145" />
        <el-table-column label="上榜月份合约" min-width="170"><template #default="scope">{{ scope.row.contracts.join("、") }}</template></el-table-column>
        <el-table-column label="已公布多头（张）" min-width="145"><template #default="scope">{{ formatPosition(scope.row.longPosition) }}</template></el-table-column>
        <el-table-column label="已公布空头（张）" min-width="145"><template #default="scope">{{ formatPosition(scope.row.shortPosition) }}</template></el-table-column>
        </el-table>
        <h3>合约与公布名次</h3>
        <el-table :data="memberDetailRows" size="small" max-height="330" class="member-position-table">
          <el-table-column prop="productDisplayName" label="品种" min-width="145" />
          <el-table-column prop="contractCode" label="合约" width="100" />
          <el-table-column label="多头（名次）" min-width="130"><template #default="scope">{{ formatPosition(scope.row.longPosition) }}（{{ scope.row.longRank ?? "—" }}）</template></el-table-column>
          <el-table-column label="空头（名次）" min-width="130"><template #default="scope">{{ formatPosition(scope.row.shortPosition) }}（{{ scope.row.shortRank ?? "—" }}）</template></el-table-column>
          <el-table-column label="发布方／适配器" min-width="210"><template #default="scope">{{ scope.row.publishers?.join("；") || scope.row.exchange }} · {{ scope.row.adapters?.join("；") || scope.row.sources.join("；") }}<small v-if="scope.row.qualityStatuses?.length"> · {{ scope.row.qualityStatuses.join("、") }}</small></template></el-table-column>
          <el-table-column label="来源摘要" min-width="135"><template #default="scope">{{ formatSourceHashes(scope.row.payloadSha256s) }}</template></el-table-column>
        </el-table>
      </details>
      <div v-if="!memberInventoryKey && !overviewLoading" class="structure-empty"><strong>暂无可选择的已公布席位</strong></div>
      <p class="member-position-note">饼图和面积图只合计已公布的方向排名；同名规范席位跨交易所汇总，交易所仍在明细中保留。未上榜和采集失败日不按零持仓处理。</p>
    </section>

    <section class="panel member-position-panel" data-test="futures-member-positions">
      <header class="structure-header">
        <div>
          <h2>期货席位分布</h2>
          <p>默认显示最近公布日持仓最大的商品。四张饼图展示当日席位分布，四张历史图展示持仓面积与已公布增减。</p>
        </div>
        <div class="member-position-filters">
          <el-select v-model="memberPositionProduct" filterable clearable placeholder="商品品种" :disabled="!memberPositionProducts.length" aria-label="席位排名品种筛选">
            <el-option v-for="item in memberPositionProducts" :key="item.productKey" :label="item.displayName" :value="item.productKey" />
          </el-select>
          <el-select v-model="memberPositionContract" clearable placeholder="月份合约" :disabled="!memberPositionContracts.length" aria-label="席位排名合约筛选">
            <el-option v-for="item in memberPositionContracts" :key="item" :label="item" :value="item" />
          </el-select>
        </div>
      </header>
      <el-alert v-if="memberPositionError || commodityRankingError" :title="memberPositionError || commodityRankingError" type="warning" :closable="false" />
      <el-alert v-if="commodityHistoryError" :title="commodityHistoryError" type="warning" :closable="false" />
      <el-skeleton v-else-if="(memberPositionLoading && !memberPositionPayload) || (commodityRankingLoading && !commodityRankingPayload)" :rows="5" animated />
      <template v-else-if="memberPositionPayload">
        <div class="member-position-coverage">
          <span>交易日：{{ selectedRankingDay }}</span>
          <span>已公布方向排名：{{ memberPositionPayload.coverage.publishedDirectionRankCount.toLocaleString("zh-CN") }}</span>
          <span v-if="memberPositionPayload.coverage.missingExchanges.length">缺失交易所：{{ memberPositionPayload.coverage.missingExchanges.join("、") }}</span>
        </div>
        <el-button v-if="memberPositionPayload.latestPublishedDay && !memberPositionPayload.available && memberPositionPayload.latestPublishedDay !== memberPositionPayload.tradingDay" size="small" @click="rankingDay = memberPositionPayload.latestPublishedDay">查看最近有排名的 {{ memberPositionPayload.latestPublishedDay }}</el-button>
        <ul v-if="memberPositionPayload.coverage.exchangeCoverage?.length" class="member-position-source-coverage" data-test="member-position-source-coverage">
          <li v-for="item in memberPositionPayload.coverage.exchangeCoverage" :key="item.exchange">
            {{ item.exchange }}：{{ item.status }} · {{ item.contractCount }} 个合约／{{ item.recordCount }} 条排名
            <template v-if="item.publishers?.length"> · 发布方 {{ item.publishers.join("；") }}</template>
            <template v-if="item.adapters?.length"> · 采集方式 {{ item.adapters.some(value => value.startsWith('official-')) ? '官网直连（历史记录另有包装来源）' : '历史AKShare包装' }}</template>
            <template v-if="item.sourceUrls?.length"> · <a :href="item.sourceUrls[0]" target="_blank" rel="noopener noreferrer">官网原始文件</a></template>
            <template v-if="item.qualityStatuses?.length"> · 质量 {{ item.qualityStatuses.join("、") }}</template>
            <template v-if="item.payloadSha256s?.length"> · SHA-256 {{ item.payloadSha256s[0].slice(0, 12) }}</template>
            <template v-if="item.retrievedAt"> · 抓取 {{ item.retrievedAt }}</template>
            <template v-if="item.error"> · {{ item.error }}</template>
          </li>
        </ul>
        <el-skeleton v-if="commodityHistoryLoading && !commodityHistoryPayload" :rows="6" animated />
        <template v-else-if="commodityHistoryPayload?.available">
          <div class="position-chart-grid commodity-pie-grid" data-test="commodity-member-pies">
            <FuturesPositionPieChart v-for="metric in commodityCharts" :key="`pie-${metric.key}`" :title="`${selectedRankingDay} ${metric.title}`" :slices="metric.pie" />
          </div>
          <div class="position-chart-grid commodity-trend-grid" data-test="commodity-member-trends">
              <FuturesPositionTrendChart v-for="metric in commodityCharts" :key="`trend-${metric.key}`" :title="`${metric.title}与增减历史`" :dates="commodityHistoryPayload.dates" :series="metric.trend" :change-totals="metric.changeTotals" area />
          </div>
          <p class="member-position-note">净多与净空只计算同一合约、同一席位同时公布多空两侧的记录；未公布方向和失败日期留空。</p>
        </template>
        <template v-if="memberPositionPayload.available">
          <details v-if="commodityRankingPayload?.rows.length" class="position-details">
            <summary>查看所选日的公布名次与合约明细</summary>
            <el-table :data="commodityRankingPayload.rows" size="small" max-height="440" class="member-position-table">
            <el-table-column prop="exchange" label="交易所" width="74" />
            <el-table-column label="品种" min-width="145"><template #default="scope">{{ selectedProduct?.displayName }}</template></el-table-column>
            <el-table-column prop="contractCode" label="合约" width="88" />
            <el-table-column prop="memberName" label="席位" min-width="150" />
            <el-table-column label="多头（名次）" min-width="130"><template #default="scope">{{ formatPosition(scope.row.longPosition) }}（{{ scope.row.longRank ?? "—" }}）</template></el-table-column>
            <el-table-column label="空头（名次）" min-width="130"><template #default="scope">{{ formatPosition(scope.row.shortPosition) }}（{{ scope.row.shortRank ?? "—" }}）</template></el-table-column>
            <el-table-column label="多头净" min-width="94"><template #default="scope">{{ formatPosition(scope.row.netLongPosition) }}</template></el-table-column>
            <el-table-column label="空头净" min-width="94"><template #default="scope">{{ formatPosition(scope.row.netShortPosition) }}</template></el-table-column>
            <el-table-column label="发布方／适配器" min-width="210"><template #default="scope">{{ scope.row.publishers?.join("；") || scope.row.exchange }} · {{ scope.row.adapters?.join("；") || scope.row.sources.join("；") }}<small v-if="scope.row.qualityStatuses?.length"> · {{ scope.row.qualityStatuses.join("、") }}</small></template></el-table-column>
            <el-table-column label="来源摘要" min-width="135"><template #default="scope">{{ formatSourceHashes(scope.row.payloadSha256s) }}</template></el-table-column>
            </el-table>
          </details>
          <div v-else-if="!memberPositionProduct" class="structure-empty"><strong>暂无可选择的已公布商品</strong><span>可再选择月份合约；排名只覆盖交易所公布的席位。</span></div>
        </template>
        <div v-else-if="!commodityHistoryPayload?.available" class="structure-empty"><strong>期货席位分布暂无可用数据</strong><span>{{ memberPositionPayload.limitations[0] }}</span></div>
        <ul class="member-position-limitations"><li v-for="item in memberPositionPayload.limitations" :key="item">{{ item }}</li></ul>
      </template>
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
.member-position-filters { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.member-position-filters .el-select { width: 146px; }
.member-position-filters .el-date-editor { width: 220px; }
.member-position-note { margin: 8px 0 0; color: var(--ml-text-secondary); font-size: 12px; }
.structure-coverage-warning { margin-bottom: 10px; }
.structure-coverage-detail { display: grid; gap: 3px; margin: -4px 0 10px; padding: 8px 12px; border-left: 3px solid var(--ml-warning, #d97706); color: var(--ml-text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.member-position-coverage { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-bottom: 8px; color: var(--ml-text-secondary); font-size: 12px; }
.member-position-source-coverage { margin: 0 0 8px; padding-left: 18px; color: var(--ml-text-disabled); font-size: 11px; }
.member-position-limitations { margin: 8px 0 0; padding-left: 18px; color: var(--ml-text-disabled); font-size: 11px; }
.position-chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 12px 0; }
.position-details { margin-top: 12px; color: var(--ml-text-secondary); font-size: 12px; }
.position-details summary { cursor: pointer; padding: 6px 0; }
.position-details h3 { font-size: 13px; }
@media (max-width: 900px) { .position-chart-grid { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .member-position-filters { justify-content: flex-start; } .member-position-filters .el-select { width: min(100%, 240px); } }
@media (max-width: 720px) { .structure-header { display: grid; } .structure-controls { justify-content: flex-start; } }
</style>
