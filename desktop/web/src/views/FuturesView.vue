<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import FuturesWeeklyHeat from "../components/futures/FuturesWeeklyHeat.vue";
import FuturesBubbleChart from "../components/futures/FuturesBubbleChart.vue";
import FuturesBubbleTimeline from "../components/futures/FuturesBubbleTimeline.vue";
import FuturesSignedBarChart from "../components/futures/FuturesSignedBarChart.vue";
import FuturesPositionSpreadAreaChart from "../components/futures/FuturesPositionSpreadAreaChart.vue";
import FuturesTripleMetricChart from "../components/futures/FuturesTripleMetricChart.vue";
import FuturesContractInfo from "../components/futures/FuturesContractInfo.vue";
import FuturesLoadingSkeleton from "../components/futures/FuturesLoadingSkeleton.vue";
import FuturesTradingDatePicker from "../components/futures/FuturesTradingDatePicker.vue";
import { sumPublishedValues } from "../domain/futuresPublishedTotals";
import type { FuturesPlaybackWindow } from "../domain/futuresPlaybackWindow";
import { ApiRequestError, apiGet, apiPost, peekCachedQuery } from "../domain/api";
import type { FuturesContractInfoResponse, FuturesDistributionMetric, FuturesDistributionResponse, FuturesPairedBubblePayload, FuturesRankingDirectory, FuturesRankingSeriesResponse, FuturesStructureRange, FuturesWeeklyHeatResponse, RankingSeries } from "../domain/futures";

const range = ref<FuturesStructureRange>("1y");
let refreshedRange: FuturesStructureRange | undefined;
const sections = [
  { id: "futures-heat", label: "多空热度" },
  { id: "futures-market", label: "市场分布" },
  { id: "futures-members", label: "席位持仓" },
  { id: "futures-product-members", label: "品种席位" },
  { id: "futures-contracts", label: "合约信息" },
  { id: "futures-questions", label: "常见问题" },
] as const;
const activeSection = ref<string>(sections[0].id);
let sectionObserver: IntersectionObserver | undefined;
const playbackDock = ref<HTMLElement>();
const playbackDockHeight = ref(76);
let playbackDockObserver: ResizeObserver | undefined;
function goToSection(id: string): void {
  activeSection.value = id;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.getElementById(id)?.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
}
const weeklyHeat = ref<FuturesWeeklyHeatResponse>();
const heatProduct = ref("");
const distributions = ref<Partial<Record<FuturesDistributionMetric, FuturesDistributionResponse>>>({});
const contractInfo = ref<FuturesContractInfoResponse>();
const selectedBubble = ref("");
const bubbleSearch = ref("");
const playbackPosition = ref(0);
const playbackWindow = ref<FuturesPlaybackWindow>("60d");
const directory = ref<FuturesRankingDirectory>();
const company = ref<FuturesRankingSeriesResponse>();
const product = ref<FuturesRankingSeriesResponse>();
const ALL_MEMBERS_KEY = "__all__";
const memberKey = ref(ALL_MEMBERS_KEY);
const dateSelectionRevision = ref(0);
const productKey = ref("");
const contractCode = ref("");
const companyProductKey = ref("");
const productMemberKey = ref("");
type ProductHover = { key: string; source: "gross" | "net"; direction: "positive" | "negative" | null };
const productHover = ref<ProductHover>({ key: "", source: "gross", direction: null });
const loading = ref(false);
const usingCachedRanking = ref(false);
const rankingVerified = ref(false);
const directoryFresh = ref(false);
const companyFresh = ref(false);
const productFresh = ref(false);
const error = ref("");
type FuturesUpdateJob = { kind: string; status: string; expectedDay: string | null; completedAt: string | null };
type FuturesUpdateStatus = { autoEnabled: boolean; checkIntervalSeconds: number;
  latestObserved: Record<string, string | null>; jobs: FuturesUpdateJob[];
  localQuality: { queryIndexStatus: string | null; contractCount: number | null;
    tdxValidCount: number | null; capitalCount: number | null };
  completedRevision: string; checkedAt: string };
const updateStatus = ref<FuturesUpdateStatus>();
const updateStatusError = ref("");
const requestingUpdate = ref(false);
let updateStatusTimer: number | undefined;
let updateStatusRequest = 0;
let pendingDataRefresh = false;
const activeUpdates = computed(() => updateStatus.value?.jobs.filter(job =>
  job.status === "RUNNING" || job.status === "QUEUED") || []);
const failedUpdates = computed(() => updateStatus.value?.jobs.filter(job =>
  ["FUTURES_OFFICIAL_DAILY_UPDATE", "FUTURES_SETTLEMENT_PARAMS_UPDATE", "FUTURES_MEMBER_RANK_UPDATE"].includes(job.kind)
  && (job.status === "PARTIAL_FAILURE" || job.status === "FAILED")) || []);
const autoSourceJobs = computed(() => updateStatus.value?.jobs.filter(job =>
  ["FUTURES_OFFICIAL_DAILY_UPDATE", "FUTURES_SETTLEMENT_PARAMS_UPDATE", "FUTURES_MEMBER_RANK_UPDATE"].includes(job.kind)) || []);
const jobLabels: Record<string, string> = {
  FUTURES_OFFICIAL_DAILY_UPDATE: "官网日行情", FUTURES_SETTLEMENT_PARAMS_UPDATE: "结算参数",
  FUTURES_MEMBER_RANK_UPDATE: "席位排名",
};
const statusLabels: Record<string, string> = {
  QUEUED: "排队", RUNNING: "更新中", PASS: "通过", PARTIAL_FAILURE: "部分缺报",
  FAILED: "失败", CANCELLED: "已取消", NOT_RUN: "未运行",
};
const expectedUpdateDay = computed(() => updateStatus.value?.jobs.find(job =>
  job.kind === "FUTURES_OFFICIAL_DAILY_UPDATE")?.expectedDay || "");
const localFileLag = computed(() => Boolean(expectedUpdateDay.value && updateStatus.value &&
  (!updateStatus.value.latestObserved.tdxLatestDay ||
   updateStatus.value.latestObserved.tdxLatestDay < expectedUpdateDay.value)));
async function checkUpdateStatus(): Promise<void> {
  const request = ++updateStatusRequest;
  try {
    const current = await apiGet<FuturesUpdateStatus>("/api/futures/update-status", undefined, { force: true });
    if (request !== updateStatusRequest) return;
    const previous = updateStatus.value?.completedRevision;
    updateStatus.value = current;
    updateStatusError.value = "";
    if (previous && current.completedRevision !== previous) {
      if (loading.value) pendingDataRefresh = true;
      else void refresh();
    }
  } catch (cause) {
    if (request === updateStatusRequest)
      updateStatusError.value = cause instanceof ApiRequestError && cause.status === 404
        ? "当前服务缺少更新状态接口，请重启服务后重试。" : "更新状态暂不可用，页面保留上次读取结果。";
  }
}
async function requestSourceUpdate(): Promise<void> {
  if (requestingUpdate.value) return;
  requestingUpdate.value = true;
  try {
    await apiPost("/api/futures/check-updates");
    await checkUpdateStatus();
  } catch (cause) {
    updateStatusError.value = `提交更新失败：${String(cause)}`;
  } finally {
    requestingUpdate.value = false;
  }
}
const refreshRevision = ref(0);
let companyRevision = 0;
let productRevision = 0;
let heatRevision = 0;
let selectingDefaults = false;
let changingProduct = false;

const bubbleDates = computed(() => distributions.value.notional?.dates || []);
const companyDates = computed(() => company.value?.dates || []);
const productDates = computed(() => product.value?.dates || []);
const playbackDates = computed(() => bubbleDates.value.length ? bubbleDates.value
  : companyDates.value.length ? companyDates.value : productDates.value);
const playbackDay = computed(() => playbackDates.value[Math.round(playbackPosition.value)] || "");
const companyIndex = computed(() => companyDates.value.indexOf(playbackDay.value));
const productIndex = computed(() => productDates.value.indexOf(playbackDay.value));
const companyRows = computed(() => signedRows(company.value, companyIndex.value, "longPosition", "shortPosition"));
const companyNetLongRows = computed(() => signedRows(company.value, companyIndex.value, "netLongPosition", ""));
const companyNetShortRows = computed(() => signedRows(company.value, companyIndex.value, "", "netShortPosition"));
const productGrossRows = computed(() => signedRows(product.value, productIndex.value, "longPosition", "shortPosition"));
const productNetRows = computed(() => signedRows(product.value, productIndex.value, "netLongPosition", "netShortPosition"));
const playbackReset = computed(() => [range.value, refreshRevision.value, selectedBubble.value, bubbleSearch.value,
  memberKey.value, productKey.value, contractCode.value, dateSelectionRevision.value].join(":"));
function selectPlaybackDay(day: string): void {
  const index = playbackDates.value.indexOf(day);
  if (index < 0) return;
  pausePlayback();
  playbackPosition.value = index;
}
function pausePlayback(): void { dateSelectionRevision.value += 1; }
const productTotals = computed(() => ({
  longPosition: sumPublishedValues(matrix(product.value, "longPosition"), productDates.value),
  shortPosition: sumPublishedValues(matrix(product.value, "shortPosition"), productDates.value),
  netLongPosition: sumPublishedValues(matrix(product.value, "netLongPosition"), productDates.value),
  netShortPosition: sumPublishedValues(matrix(product.value, "netShortPosition"), productDates.value),
}));
function totalsExtent(first: Array<number | null>, second: Array<number | null>): number {
  return Math.max(1, ...[...first, ...second].filter((value): value is number => typeof value === "number" && Number.isFinite(value)));
}
const productGrossSummary = computed(() => ({
  positive: productTotals.value.longPosition[productIndex.value] ?? null,
  negative: productTotals.value.shortPosition[productIndex.value] ?? null,
  extent: totalsExtent(productTotals.value.longPosition, productTotals.value.shortPosition),
}));
const productNetSummary = computed(() => ({
  positive: productTotals.value.netLongPosition[productIndex.value] ?? null,
  negative: productTotals.value.netShortPosition[productIndex.value] ?? null,
  extent: totalsExtent(productTotals.value.netLongPosition, productTotals.value.netShortPosition),
}));
const productHoverFacts = computed(() => {
  const key = productHover.value.key;
  if (!key) return null;
  const name = matrix(product.value, "gross").find(item => item.key === key)?.name || key;
  const current = (metric: string) => matrix(product.value, metric).find(item => item.key === key)?.values[productIndex.value] ?? null;
  return { key, name, long: current("longPosition"), short: current("shortPosition"),
    netLong: current("netLongPosition"), netShort: current("netShortPosition") };
});
const productHoverDirection = computed(() => {
  const labels = productHover.value.source === "gross" ? ["多头持仓", "空头持仓"] : ["净多持仓", "净空持仓"];
  return productHover.value.direction === "positive" ? labels[0] : labels[1];
});
const activeProduct = computed(() => directory.value?.products.find(item => item.key === productKey.value));
const selectedCompanyName = computed(() => memberKey.value === ALL_MEMBERS_KEY
  ? "全部席位" : directory.value?.members.find(item => item.key === memberKey.value)?.name || "所选公司");
const selectedProductName = computed(() => directory.value?.products.find(item => item.key === productKey.value)?.name || "所选品种");
const selectedCompanyProductName = computed(() => matrix(company.value, "gross")
  .find(item => item.key === companyProductKey.value)?.name || "所选品种");
const selectedProductMemberName = computed(() => matrix(product.value, "gross")
  .find(item => item.key === productMemberKey.value)?.name || "全部席位");
const selectedBubbleName = computed(() => distributions.value.notional?.series
  .find(item => item.key === selectedBubble.value)?.name || "全部品种");
const companyExtent = computed(() => metricExtent(company.value, "longPosition", "shortPosition"));
const companyNetLongExtent = computed(() => metricExtent(company.value, "netLongPosition", ""));
const companyNetShortExtent = computed(() => metricExtent(company.value, "", "netShortPosition"));
const productGrossExtent = computed(() => metricExtent(product.value, "longPosition", "shortPosition"));
const productNetExtent = computed(() => metricExtent(product.value, "netLongPosition", "netShortPosition"));
const showingCachedSnapshot = computed(() => usingCachedRanking.value
  || [directory.value, company.value, product.value, ...Object.values(distributions.value)]
    .some(payload => payload?.snapshotStatus && payload.snapshotStatus !== "CURRENT"));
const exchangeNames: Record<string, string> = {
  SHFE: "上期所", INE: "能源中心", DCE: "大商所", CZCE: "郑商所", GFEX: "广期所", CFFEX: "中金所",
};
const bubbleSourceGaps = computed(() => {
  const turnover = distributions.value.turnover;
  const day = playbackDay.value;
  const index = turnover?.dates.indexOf(day) ?? -1;
  if (!turnover || index < 0) return [];
  return (turnover.coverage[index]?.missingExchanges || []).map(exchange => {
    const products = turnover.series.filter(item => item.key.startsWith(`${exchange}.`));
    let lastVerifiedDay = "";
    for (let previous = index - 1; previous >= 0; previous--) {
      if (turnover.coverage[previous]?.missingExchanges?.includes(exchange)) continue;
      if (!products.some(item => typeof item.values[previous] === "number")) continue;
      lastVerifiedDay = turnover.dates[previous];
      break;
    }
    return { exchange, name: exchangeNames[exchange] || exchange, count: products.length, lastVerifiedDay };
  });
});

function sameDistributionSnapshot(items: FuturesDistributionResponse[]): boolean {
  if (items.length !== 4 || items.some(item => !item.available)) return false;
  const [first] = items;
  const versions = items.map(item => item.dataVersion);
  if (versions.some(Boolean) && !versions.every(version => version === versions[0])) return false;
  return items.every(item => item.dates.length === first.dates.length
    && item.dates.every((day, index) => day === first.dates[index]));
}

async function fetchFutures<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  for (let attempt = 0; attempt < 5; attempt++) {
    try { return await apiGet<T>(path, params, { force: true, persist: path.includes("ranking-") }); }
    catch (cause) {
      if (attempt === 4 || !/\(5\d\d\)|Cannot open file/i.test(String(cause))) throw cause;
      await new Promise(resolve => setTimeout(resolve, Math.min(2000, 250 * 2 ** attempt)));
    }
  }
  throw new Error("官网期货数据请求未完成");
}

const marketBubble = computed<FuturesPairedBubblePayload | undefined>(() => {
  const notional = distributions.value.notional;
  const returns = distributions.value.return;
  const turnover = distributions.value.turnover;
  const deposit = distributions.value.deposit;
  if (!notional || !returns || !turnover || !deposit) return undefined;
  const returnDays = new Map(returns.dates.map((day, index) => [day, index]));
  const returnRows = new Map(returns.series.map(row => [row.key, row]));
  const turnoverDays = new Map(turnover.dates.map((day, index) => [day, index]));
  const turnoverRows = new Map(turnover.series.map(row => [row.key, row]));
  const depositDays = new Map(deposit.dates.map((day, index) => [day, index]));
  const depositRows = new Map(deposit.series.map(row => [row.key, row]));
  const depositMissingExchanges = new Map(deposit.coverage.map(item =>
    [item.tradeDate, new Set(item.missingExchanges || [])]));
  const value = (raw: number | null | undefined, divisor: number): number | null =>
    typeof raw === "number" && Number.isFinite(raw) ? raw / divisor : null;
  return {
    dates: notional.dates,
    series: notional.series.map(row => ({
      key: row.key, name: row.name,
      horizontalValues: notional.dates.map(day => value(
        turnoverRows.get(row.key)?.values[turnoverDays.get(day) ?? -1], 1e8,
      )),
      verticalValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.directionSumValues?.[returnDays.get(day) ?? -1], 1,
      )),
      dailyReturnValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.values[returnDays.get(day) ?? -1], 1,
      )),
      tenDayReturnValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.tenDayReturnValues?.[returnDays.get(day) ?? -1], 1,
      )),
      directionUpCountValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.directionUpCountValues?.[returnDays.get(day) ?? -1], 1,
      )),
      directionDownCountValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.directionDownCountValues?.[returnDays.get(day) ?? -1], 1,
      )),
      directionFlatCountValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.directionFlatCountValues?.[returnDays.get(day) ?? -1], 1,
      )),
      directionWindowCountValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.directionWindowCountValues?.[returnDays.get(day) ?? -1], 1,
      )),
      directionStatusValues: notional.dates.map(day =>
        returnRows.get(row.key)?.directionStatusValues?.[returnDays.get(day) ?? -1] || null),
      directionReasonValues: notional.dates.map(day => {
        const index = returnDays.get(day) ?? -1;
        const product = returnRows.get(row.key);
        return product?.directionReasonValues?.[index]
          || (typeof product?.directionSumValues?.[index] === "number" ? null : "方向累计缺少已核验窗口或快照尚未就绪");
      }),
      shortMove3Values: notional.dates.map(day => value(
        returnRows.get(row.key)?.shortMove3Values?.[returnDays.get(day) ?? -1], 1,
      )),
      shortRankValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.shortRankValues?.[returnDays.get(day) ?? -1], 1,
      )),
      shortSampleCountValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.shortSampleCountValues?.[returnDays.get(day) ?? -1], 1,
      )),
      shortCompareStatusValues: notional.dates.map(day =>
        returnRows.get(row.key)?.shortCompareStatusValues?.[returnDays.get(day) ?? -1] || null),
      sizeValues: notional.dates.map(day => value(
        depositRows.get(row.key)?.values[depositDays.get(day) ?? -1], 1e8,
      )),
      coverageValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.tenDayCoverageValues?.[returnDays.get(day) ?? -1], 1,
      )),
      windowMinValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.tenDayWindowMinValues?.[returnDays.get(day) ?? -1], 1,
      )),
      windowMaxValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.tenDayWindowMaxValues?.[returnDays.get(day) ?? -1], 1,
      )),
      shortContractCountValues: notional.dates.map(day => value(
        returnRows.get(row.key)?.shortWindowContractCountValues?.[returnDays.get(day) ?? -1], 1,
      )),
      depositReasonValues: notional.dates.map(day => {
        const index = depositDays.get(day) ?? -1;
        const product = depositRows.get(row.key);
        if (typeof product?.values[index] === "number") return null;
        return product?.depositReasonValues?.[index]
          || (depositMissingExchanges.get(day)?.has(row.key.split(".", 1)[0])
            ? "该所官网日行情未通过核验" : null);
      }),
      missingMarginContractCountValues: notional.dates.map(day => value(
        depositRows.get(row.key)?.missingMarginContractCountValues?.[depositDays.get(day) ?? -1], 1,
      )),
      marginPublicationDayValues: notional.dates.map(day =>
        depositRows.get(row.key)?.marginPublicationDayValues?.[depositDays.get(day) ?? -1] || null,
      ),
    })),
    xLabel: "当日成交额（亿元）",
    yLabel: "近1～60交易日方向累计（百分点）",
    sizeLabel: "沉淀资金",
    xUnit: "亿元",
    yUnit: "百分点",
    sizeUnit: "亿元",
    ySigned: true,
  };
});
const bubbleMarginGaps = computed(() => {
  const bubble = marketBubble.value;
  const index = bubble?.dates.indexOf(playbackDay.value) ?? -1;
  if (!bubble || index < 0) return [];
  const counts = new Map<string, number>();
  for (const item of bubble.series) {
    const x = item.horizontalValues[index], y = item.verticalValues[index];
    const missingMargin = item.missingMarginContractCountValues?.[index];
    if (item.sizeValues[index] !== null
      || typeof x !== "number" || !Number.isFinite(x)
      || typeof y !== "number" || !Number.isFinite(y)
      || typeof missingMargin !== "number" || !Number.isFinite(missingMargin) || missingMargin <= 0) continue;
    const exchange = item.key.split(".", 1)[0];
    counts.set(exchange, (counts.get(exchange) || 0) + 1);
  }
  return [...counts].map(([exchange, count]) => ({ exchange, name: exchangeNames[exchange] || exchange, count }));
});
const marketHistory = computed(() => {
  const bubble = marketBubble.value;
  const notional = distributions.value.notional;
  const turnover = distributions.value.turnover;
  const deposit = distributions.value.deposit;
  const returns = distributions.value.return;
  if (!bubble || !notional || !turnover || !deposit || !returns) return undefined;
  const selected = bubble.series.find(item => item.key === selectedBubble.value);
  const turnoverDays = new Map(turnover.dates.map((day, index) => [day, index]));
  const amount = (value: number | null | undefined): number | null =>
    typeof value === "number" && Number.isFinite(value) ? value / 1e8 : null;
  if (selected) {
    return {
      dates: bubble.dates,
      turnoverValues: selected.horizontalValues,
      returnValues: selected.tenDayReturnValues || bubble.dates.map(() => null),
      directionValues: selected.verticalValues,
      depositValues: selected.sizeValues,
      returnCoverageLabels: selected.coverageValues?.map((value, index) => value === null
        ? "" : `品种加权价格窗口：${selected.windowMaxValues?.[index] ?? '—'}个实际交易日；窗口覆盖：${(value * 100).toFixed(2)}%`) || [],
      directionCoverageLabels: bubble.dates.map((_, index) => selected.directionStatusValues?.[index] === "SHORT"
        ? `短窗口：${selected.directionWindowCountValues?.[index] ?? "—"}个实际交易日`
        : selected.directionReasonValues?.[index] || ""),
      title: `${selected.name}市场与方向走势`,
    };
  }
  const notionalDays = new Map(notional.dates.map((day, index) => [day, index]));
  const depositDays = new Map(deposit.dates.map((day, index) => [day, index]));
  const returnDays = new Map(returns.dates.map((day, index) => [day, index]));
  const returnValues: Array<number | null> = [];
  const returnCoverageLabels: string[] = [];
  const directionValues: Array<number | null> = [];
  const directionCoverageLabels: string[] = [];
  for (const day of bubble.dates) {
    const index = notionalDays.get(day) ?? -1;
    const activeKeys = new Set(notional.series.filter(item =>
      typeof item.values[index] === "number" && item.values[index] > 0).map(item => item.key));
    const active = bubble.series.filter(item => activeKeys.has(item.key));
    const valid = active.map(item => item.tenDayReturnValues?.[index])
      .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
    const shortProducts = active.filter(item => {
      const window = item.windowMaxValues?.[index];
      return typeof window === "number" && window > 0 && window < 10;
    }).length;
    const quality = returns.coverage[returnDays.get(day) ?? -1]?.dataQualityStatus;
    returnValues.push(quality === "PASS" && valid.length
      ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null);
    returnCoverageLabels.push(`收益覆盖：${valid.length}/${active.length} 个正市值品种；短窗品种：${shortProducts} 个`);
    const validDirections = active.map(item => item.verticalValues[index])
      .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
    directionValues.push(quality === "PASS" && active.length > 0 && validDirections.length === active.length
      ? validDirections.reduce((sum, value) => sum + value, 0) / validDirections.length : null);
    const shortDirections = active.filter(item => item.directionStatusValues?.[index] === "SHORT").length;
    directionCoverageLabels.push(`方向覆盖：${validDirections.length}/${active.length} 个正市值品种；短窗口：${shortDirections} 个`);
  }
  return {
    dates: bubble.dates,
    turnoverValues: bubble.dates.map(day => amount(turnover.totals[turnoverDays.get(day) ?? -1])),
    returnValues,
    directionValues,
    directionCoverageLabels,
    depositValues: bubble.dates.map(day => amount(deposit.totals[depositDays.get(day) ?? -1])),
    returnCoverageLabels,
    title: "全部品种市场与方向走势",
  };
});
function matrix(payload: FuturesRankingSeriesResponse | undefined, metric: string): RankingSeries[] {
  return payload?.metrics?.[metric] || [];
}
function historyValues(payload: FuturesRankingSeriesResponse | undefined, key: string, metric: string): Array<number | null> {
  const item = matrix(payload, metric).find(row => row.key === key);
  return item?.values || payload?.dates.map(() => null) || [];
}
function productHistoryValues(metric: keyof typeof productTotals.value): Array<number | null> {
  return productMemberKey.value ? historyValues(product.value, productMemberKey.value, metric) : productTotals.value[metric];
}
function formatPublished(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString("zh-CN");
}
function updateProductHover(source: "gross" | "net", hover: { key: string; direction: "positive" | "negative" | null }): void {
  if (hover.key) productHover.value = { ...hover, source };
  else if (productHover.value.source === source) productHover.value = { key: "", source, direction: null };
}
function signedRows(payload: FuturesRankingSeriesResponse | undefined, index: number,
  positive: string, negative: string): Array<{ key: string; name: string; positive: number | null; negative: number | null }> {
  const up = matrix(payload, positive);
  const down = new Map(matrix(payload, negative).map(row => [row.key, row]));
  const order = matrix(payload, "gross").length ? matrix(payload, "gross") : up;
  return order.map(item => ({ key: item.key, name: item.name,
    positive: up.find(row => row.key === item.key)?.values[index] ?? null,
    negative: down.get(item.key)?.values[index] ?? null }))
    .filter(row => (typeof row.positive === "number" && Number.isFinite(row.positive) && row.positive !== 0)
      || (typeof row.negative === "number" && Number.isFinite(row.negative) && row.negative !== 0));
}
function metricExtent(payload: FuturesRankingSeriesResponse | undefined, first: string, second: string): number {
  let maximum = 1;
  for (const metric of [first, second]) for (const row of matrix(payload, metric))
    for (const value of row.values) if (typeof value === "number" && Number.isFinite(value)) maximum = Math.max(maximum, Math.abs(value));
  return maximum;
}
async function loadCompany(preserveExisting = false): Promise<boolean> {
  const revision = ++companyRevision;
  if (!preserveExisting) company.value = undefined;
  if (!memberKey.value) return false;
  try {
    const response = await fetchFutures<FuturesRankingSeriesResponse>("/api/futures/ranking-series",
      { view: "member-products", member_key: memberKey.value, range: range.value });
    if (revision !== companyRevision) return false;
    company.value = response;
    companyFresh.value = true;
    if (!matrix(response, "gross").some(row => row.key === companyProductKey.value))
      companyProductKey.value = matrix(response, "gross")[0]?.key || "";
    return true;
  } catch (cause) { if (revision === companyRevision) error.value = String(cause); return false; }
}

async function loadProduct(preserveExisting = false): Promise<boolean> {
  const revision = ++productRevision;
  if (!preserveExisting) product.value = undefined;
  if (!productKey.value) return false;
  try {
    const response = await fetchFutures<FuturesRankingSeriesResponse>("/api/futures/ranking-series",
      { view: "product-members", product_key: productKey.value,
        contract_code: contractCode.value || undefined, range: range.value });
    if (revision !== productRevision) return false;
    product.value = response;
    productFresh.value = true;
    if (productMemberKey.value && !matrix(response, "gross").some(row => row.key === productMemberKey.value))
      productMemberKey.value = "";
    return true;
  } catch (cause) { if (revision === productRevision) error.value = String(cause); return false; }
}

async function loadHeat(revision = refreshRevision.value): Promise<void> {
  const request = ++heatRevision;
  const response = await fetchFutures<FuturesWeeklyHeatResponse>("/api/futures/weekly-heat",
    { range: range.value, product_key: heatProduct.value || undefined });
  if (revision === refreshRevision.value && request === heatRevision) weeklyHeat.value = response;
}
async function loadContractInfo(revision: number): Promise<void> {
  const response = await fetchFutures<FuturesContractInfoResponse>("/api/futures/contract-info");
  if (revision === refreshRevision.value) contractInfo.value = response;
}
async function loadDistributions(revision: number): Promise<void> {
  const fetchMetric = (metric: FuturesDistributionMetric) => fetchFutures<FuturesDistributionResponse>(
    "/api/futures/market-distributions", { metric, range: range.value, window: 20 },
  );
  for (let attempt = 0; attempt < 2; attempt++) {
    // The first request populates the shared server-side source cache.
    const notional = await fetchMetric("notional");
    if (revision !== refreshRevision.value) return;
    const [turnover, returns, deposit] = await Promise.all([
      fetchMetric("turnover"), fetchMetric("return"), fetchMetric("deposit"),
    ]);
    if (revision !== refreshRevision.value) return;
    if (!sameDistributionSnapshot([notional, turnover, returns, deposit])) continue;
    distributions.value = { notional, turnover, return: returns, deposit };
    return;
  }
  throw new Error("期货分布数据版本尚未一致，请稍后刷新");
}
async function prefillDistributions(revision: number): Promise<void> {
  const metrics: FuturesDistributionMetric[] = ["notional", "turnover", "return", "deposit"];
  const responses = await Promise.all(metrics.map(async metric => {
    try {
      const response = await apiGet<FuturesDistributionResponse>("/api/futures/market-distributions",
        { metric, range: range.value, window: 20, snapshot_only: 1 }, { force: true });
      return response.available && response.dates.length ? response : undefined;
    } catch { return undefined; /* The verified request remains authoritative. */ }
  }));
  if (revision !== refreshRevision.value || !responses.every(Boolean)) return;
  const [notional, turnover, returns, deposit] = responses as FuturesDistributionResponse[];
  if (sameDistributionSnapshot([notional, turnover, returns, deposit]))
    distributions.value = { notional, turnover, return: returns, deposit };
}
async function prefillRankings(revision: number): Promise<void> {
  let cachedDirectory = await peekCachedQuery<FuturesRankingDirectory>("/api/futures/ranking-directory");
  if (!cachedDirectory) {
    try { cachedDirectory = await apiGet<FuturesRankingDirectory>("/api/futures/ranking-directory",
      { snapshot_only: 1 }, { force: true }); } catch { /* An empty or unavailable server snapshot leaves the skeleton visible. */ }
  }
  if (!cachedDirectory || revision !== refreshRevision.value || rankingVerified.value) return;
  if (!directoryFresh.value) {
    directory.value = cachedDirectory;
    usingCachedRanking.value = true;
    selectingDefaults = true;
    if (!memberKey.value) memberKey.value = ALL_MEMBERS_KEY;
    if (!productKey.value) productKey.value = cachedDirectory.products[0]?.key || "";
    selectingDefaults = false;
  }
  const member = memberKey.value, cachedProductKey = productKey.value, contract = contractCode.value, selectedRange = range.value;
  const cachedRanking = async (params: Record<string, string | undefined>): Promise<FuturesRankingSeriesResponse | undefined> => {
    const local = await peekCachedQuery<FuturesRankingSeriesResponse>("/api/futures/ranking-series", params);
    if (local) return local;
    try { return await apiGet<FuturesRankingSeriesResponse>("/api/futures/ranking-series",
      { ...params, snapshot_only: 1 }, { force: true }); } catch { return undefined; }
  };
  const [cachedCompany, cachedProduct] = await Promise.all([
    member ? cachedRanking({ view: "member-products", member_key: member, range: selectedRange }) : undefined,
    cachedProductKey ? cachedRanking({ view: "product-members", product_key: cachedProductKey,
      contract_code: contract || undefined, range: selectedRange }) : undefined,
  ]);
  if (revision !== refreshRevision.value || rankingVerified.value) return;
  if (member === memberKey.value && cachedCompany?.available && !companyFresh.value) {
    company.value = cachedCompany;
    usingCachedRanking.value = true;
  }
  if (cachedProductKey === productKey.value && contract === contractCode.value && cachedProduct?.available && !productFresh.value) {
    product.value = cachedProduct;
    usingCachedRanking.value = true;
  }
}
async function loadDirectoryAndRankings(revision: number): Promise<void> {
  const response = await fetchFutures<FuturesRankingDirectory>("/api/futures/ranking-directory");
  if (revision !== refreshRevision.value) return;
  const previousMember = memberKey.value, previousProduct = productKey.value;
  directory.value = response;
  directoryFresh.value = true;
  selectingDefaults = true;
  if (memberKey.value !== ALL_MEMBERS_KEY && !response.members.some(item => item.key === memberKey.value))
    memberKey.value = ALL_MEMBERS_KEY;
  if (!response.products.some(item => item.key === productKey.value)) productKey.value = response.products[0]?.key || "";
  selectingDefaults = false;
  const [freshCompany, freshProduct] = await Promise.all([
    loadCompany(previousMember === memberKey.value && Boolean(company.value)),
    loadProduct(previousProduct === productKey.value && Boolean(product.value)),
  ]);
  if (revision === refreshRevision.value && freshCompany && freshProduct) {
    rankingVerified.value = true;
    usingCachedRanking.value = false;
  }
}
async function refresh(): Promise<void> {
  pausePlayback();
  const preserveExisting = refreshedRange === range.value;
  refreshedRange = range.value;
  const revision = ++refreshRevision.value;
  loading.value = true; error.value = ""; usingCachedRanking.value = false; rankingVerified.value = false;
  directoryFresh.value = false; companyFresh.value = false; productFresh.value = false;
  if (!preserveExisting) {
    weeklyHeat.value = undefined; distributions.value = {}; contractInfo.value = undefined;
    company.value = undefined; product.value = undefined;
  }
  const distributionWork = async () => {
    if (!preserveExisting) await prefillDistributions(revision);
    if (revision === refreshRevision.value) await loadDistributions(revision);
  };
  const outcomes = await Promise.allSettled([
    loadHeat(revision), loadDirectoryAndRankings(revision), loadContractInfo(revision),
    ...(!preserveExisting ? [prefillRankings(revision)] : []), distributionWork(),
  ]);
  if (revision !== refreshRevision.value) return;
  const failures = outcomes.filter((outcome): outcome is PromiseRejectedResult => outcome.status === "rejected");
  if (failures.length) error.value = failures.map(outcome => String(outcome.reason)).join("；");
  loading.value = false;
  if (pendingDataRefresh) { pendingDataRefresh = false; void refresh(); }
}
watch(playbackDates, (dates, previousDates) => {
  const selectedDay = previousDates[Math.round(playbackPosition.value)];
  playbackPosition.value = selectedDay && dates.includes(selectedDay)
    ? dates.indexOf(selectedDay) : Math.max(0, dates.length - 1);
});
watch(playbackWindow, window => { range.value = window === "3y" ? "3y" : "1y"; });
watch(range, () => { void refresh(); });
watch(heatProduct, () => { void loadHeat().catch(cause => { error.value = String(cause); }); });
watch(memberKey, () => { if (!selectingDefaults) void loadCompany(); }, { flush: "sync" });
watch(productKey, () => { if (selectingDefaults) return; changingProduct = true; productMemberKey.value = ""; contractCode.value = ""; changingProduct = false; void loadProduct(); }, { flush: "sync" });
watch(contractCode, () => { if (!changingProduct) { productMemberKey.value = ""; void loadProduct(); } }, { flush: "sync" });
watch([productKey, contractCode, range], () => { productHover.value = { key: "", source: "gross", direction: null }; });
function checkVisibleUpdateStatus(): void {
  if (document.visibilityState === "visible") void checkUpdateStatus();
}
onMounted(() => {
  void refresh();
  void checkUpdateStatus();
  updateStatusTimer = window.setInterval(checkVisibleUpdateStatus, 30_000);
  document.addEventListener("visibilitychange", checkVisibleUpdateStatus);
  if (playbackDock.value && typeof ResizeObserver !== "undefined") {
    playbackDockObserver = new ResizeObserver(entries => {
      if (entries[0]) playbackDockHeight.value = Math.ceil(entries[0].target.getBoundingClientRect().height);
    });
    playbackDockObserver.observe(playbackDock.value);
  }
  if (typeof IntersectionObserver === "undefined") return;
  sectionObserver = new IntersectionObserver(entries => {
    const visible = entries.filter(entry => entry.isIntersecting)
      .sort((first, second) => Math.abs(first.boundingClientRect.top - 100) - Math.abs(second.boundingClientRect.top - 100));
    if (visible[0]) activeSection.value = visible[0].target.id;
  }, { rootMargin: `-70px 0px -${Math.round(window.innerHeight * .55)}px 0px`, threshold: 0 });
  sections.forEach(section => {
    const element = document.getElementById(section.id);
    if (element) sectionObserver?.observe(element);
  });
});
onBeforeUnmount(() => {
  updateStatusRequest++;
  document.removeEventListener("visibilitychange", checkVisibleUpdateStatus);
  sectionObserver?.disconnect(); playbackDockObserver?.disconnect();
  if (updateStatusTimer !== undefined) window.clearInterval(updateStatusTimer);
});
</script>

<template>
  <main class="futures-page" :style="{ '--futures-dock-clearance': `${playbackDockHeight}px` }" data-test="futures-page">
    <header class="page-heading"><h1>期货数据</h1><div class="controls"><el-button :disabled="loading" :aria-busy="loading" @click="refresh"><svg class="refresh-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M19.1 8a8 8 0 0 0-13.2-2L4 8m16 8-1.9 2a8 8 0 0 1-13.2-2" /></svg>重载页面数据</el-button></div></header>
    <nav class="section-nav" aria-label="期货数据分区" data-test="futures-section-nav"><button v-for="section in sections" :key="section.id" type="button" :class="{ active: activeSection === section.id }" :aria-current="activeSection === section.id ? 'location' : undefined" @click="goToSection(section.id)">{{ section.label }}</button></nav>
    <div v-if="updateStatus" class="futures-update-summary" data-test="futures-update-summary">
      <span v-if="activeUpdates.length">后台正在更新 {{ activeUpdates.length }} 类数据；完成后本页自动重载。</span>
      <span v-else-if="failedUpdates.length">最近有 {{ failedUpdates.length }} 类来源未通过核验；已保存数据继续显示，后台按间隔重试。</span>
      <span v-else>{{ updateStatus.autoEnabled ? '服务按交易日自动检查更新' : '服务自动更新已关闭' }}<template v-if="expectedUpdateDay"> · 待核对交易日 {{ expectedUpdateDay }}</template></span>
      <small>来源最晚观测：期货通 {{ updateStatus.latestObserved.tdxLatestDay || '暂无' }} · 官网日行情 {{ updateStatus.latestObserved.officialTurnoverLatestDay || '暂无' }} · 结算参数 {{ updateStatus.latestObserved.officialMarginLatestDay || '暂无' }}。单项最新日期不代表六所全覆盖。</small>
      <small v-if="localFileLag">期货通本地日线尚未到 {{ expectedUpdateDay }}；请在已登录的期货通下载盘后数据。下载完成后可点“立即检查更新”，后台会增量导入并核验。</small>
      <small v-if="updateStatus.localQuality?.contractCount && updateStatus.localQuality.tdxValidCount === 0">当前本地合约日线质量通过数为 0，依赖本地价格的衍生值将保持空缺，请查看管理台导入与质量结果。</small>
      <div class="update-source-status" aria-label="期货各来源更新状态"><span v-for="job in autoSourceJobs" :key="job.kind">{{ jobLabels[job.kind] }}：{{ statusLabels[job.status] || job.status }}<template v-if="job.expectedDay">（目标 {{ job.expectedDay }}）</template></span></div>
      <button type="button" :disabled="requestingUpdate" @click="requestSourceUpdate">{{ requestingUpdate ? '提交中…' : '立即检查更新' }}</button>
    </div>
    <el-alert v-if="updateStatusError" class="futures-error-notice" :title="updateStatusError" type="warning" show-icon :closable="false" />
    <el-alert v-if="error" class="futures-error-notice" :title="error" type="error" show-icon :closable="false" />
    <el-alert v-if="showingCachedSnapshot" class="futures-status-notice" title="正在核对官网新数据，暂显示上次已核验的交易日快照" type="info" show-icon :closable="false" />
    <section id="futures-heat" class="heat-section"><FuturesWeeklyHeat v-if="weeklyHeat?.available" :payload="weeklyHeat"><template #controls><el-select v-model="heatProduct" filterable aria-label="多空热度品种" placeholder="全部期货"><el-option label="全部期货" value="" /><el-option v-for="item in distributions.notional?.series || []" :key="item.key" :label="item.name" :value="item.key" /></el-select></template></FuturesWeeklyHeat><div v-else-if="loading && !weeklyHeat" class="panel"><FuturesLoadingSkeleton kind="heat" /></div><div v-else class="panel empty-panel">{{ weeklyHeat?.limitations?.[0] || '暂无可用多空热度' }}</div></section>

    <section id="futures-market" class="market-distributions" data-test="futures-market-distributions">
      <section class="panel distribution-card" data-test="futures-notional-return-distribution">
        <header><h2>成交额、方向累计与沉淀资金分布</h2><div class="selection-context" data-test="futures-bubble-selection"><span>{{ selectedBubbleName }}</span><button v-if="selectedBubble" type="button" aria-label="恢复全部品种走势" title="恢复全部品种" @click="selectedBubble = ''"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8m0-8-8 8" /></svg></button></div></header>
        <template v-if="marketBubble && marketHistory"><div class="bubble-tools"><el-select v-model="selectedBubble" filterable clearable aria-label="成交额与涨跌分布选择品种" placeholder="全部品种 · 选择品种查看历史"><el-option v-for="item in marketBubble.series" :key="item.key" :label="item.name" :value="item.key" /></el-select><el-input v-model="bubbleSearch" clearable aria-label="成交额与涨跌分布搜索气泡" placeholder="搜索或高亮品种" /></div>
          <div class="bubble-legend" aria-label="气泡状态说明"><span><i class="bubble-legend-dot solid" aria-hidden="true" />实心：成交额、方向累计与沉淀资金均有值</span><span><i class="bubble-legend-dot hollow" aria-hidden="true" />空心：指标缺报，悬浮可查看原因</span><span>红圈↑／绿圈↓：近3日相对上涨／下跌前10%，不是趋势预测</span><span>小额资金气泡差异已增强；悬浮查看精确金额</span></div>
          <div v-if="bubbleSourceGaps.length" class="bubble-source-gap" data-test="futures-bubble-source-gap" role="status">
            <p v-for="gap in bubbleSourceGaps" :key="gap.exchange">{{ playbackDay }} {{ gap.name }} {{ gap.count }} 个品种的官网逐合约日行情未通过核验。空心气泡仅沿用{{ gap.lastVerifiedDay || '此前有效交易日' }}的显示位置和大小；当天成交额、方向累计和沉淀资金保持缺报。</p>
          </div>
          <div v-if="bubbleMarginGaps.length" class="bubble-margin-gap" data-test="futures-bubble-margin-gap" role="status">
            <p v-for="gap in bubbleMarginGaps" :key="gap.exchange">{{ playbackDay }} {{ gap.name }} {{ gap.count }} 个品种的沉淀资金缺报：有持仓月份合约缺所选日适用的投机保证金率。气泡因此为空心，品种及已核验的成交额、方向累计仍显示；悬浮气泡可查看每个品种的缺项数。</p>
          </div>
          <div class="distribution-visuals"><FuturesBubbleChart :payload="marketBubble" :position="playbackPosition" :selected-keys="selectedBubble ? [selectedBubble] : []" :search="bubbleSearch" @select="selectedBubble = $event" /><FuturesTripleMetricChart :dates="marketHistory.dates" :turnover-values="marketHistory.turnoverValues" :return-values="marketHistory.returnValues" :direction-values="marketHistory.directionValues" :direction-coverage-labels="marketHistory.directionCoverageLabels" :deposit-values="marketHistory.depositValues" :return-coverage-labels="marketHistory.returnCoverageLabels" :title="marketHistory.title" /></div>
        </template>
        <div v-else-if="loading" class="distribution-visuals"><FuturesLoadingSkeleton kind="bubble" /><FuturesLoadingSkeleton kind="overview" /></div>
        <div v-else class="loading-chart">暂无可用官网分布</div>
      </section>
    </section>

    <section id="futures-members" class="panel seat-section" data-test="futures-member-distribution"><header><h2>席位持仓分布</h2><div class="controls"><el-select v-model="memberKey" filterable aria-label="期货公司" placeholder="选择期货公司"><el-option label="全部席位" :value="ALL_MEMBERS_KEY" /><el-option v-for="item in directory?.members || []" :key="item.key" :label="item.name" :value="item.key" /></el-select><FuturesTradingDatePicker :dates="companyDates" :model-value="playbackDay" label="席位持仓日期" @open="pausePlayback" @update:model-value="selectPlaybackDay" /></div></header>
      <div class="member-visuals"><FuturesSignedBarChart v-if="company" :rows="companyRows" :loading="loading" :extent="companyExtent" :selected-key="companyProductKey" title="多头和空头" positive-label="多头持仓" negative-label="空头持仓" @select="companyProductKey = $event" /><div v-else-if="loading" class="skeleton-panel"><FuturesLoadingSkeleton kind="ranking" /></div><div v-else class="loading-chart">暂无已公布席位</div><FuturesPositionSpreadAreaChart :loading="loading" v-if="company" :dates="companyDates" :long-values="historyValues(company,companyProductKey,'longPosition')" :short-values="historyValues(company,companyProductKey,'shortPosition')" :title="`${selectedCompanyName} · ${selectedCompanyProductName}多空持仓历史`" long-label="多头持仓" short-label="空头持仓" /><div v-else-if="loading" class="skeleton-panel"><FuturesLoadingSkeleton kind="overview" /></div></div>
      <div v-if="company" class="member-net-visuals"><FuturesSignedBarChart :rows="companyNetLongRows" :loading="loading" :extent="companyNetLongExtent" :selected-key="companyProductKey" title="净多" positive-label="净多持仓" negative-label="" :show-negative="false" @select="companyProductKey = $event" /><FuturesSignedBarChart :rows="companyNetShortRows" :loading="loading" :extent="companyNetShortExtent" :selected-key="companyProductKey" title="净空" positive-label="" negative-label="净空持仓" :show-positive="false" @select="companyProductKey = $event" /></div>
      <div v-else-if="loading" class="member-net-visuals"><div v-for="item in 2" :key="item" class="skeleton-panel"><FuturesLoadingSkeleton kind="ranking" /></div></div>
    </section>

    <section id="futures-product-members" class="panel seat-section" data-test="futures-product-member-distribution">
      <header><div class="section-heading"><h2>期货席位分布</h2><div v-if="product" class="selection-context" data-test="futures-seat-selection"><span>{{ selectedProductMemberName }}</span><button v-if="productMemberKey" type="button" aria-label="恢复全部席位历史" title="恢复全部席位" @click="productMemberKey = ''"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8m0-8-8 8" /></svg></button></div></div><div class="controls product-filters">
        <el-select v-model="productKey" filterable aria-label="期货品种" placeholder="选择品种"><el-option v-for="item in directory?.products || []" :key="item.key" :label="item.name" :value="item.key" /></el-select>
        <el-select v-model="contractCode" filterable aria-label="合约"><el-option label="全部合约" value="" /><el-option v-for="code in activeProduct?.contracts || []" :key="code" :label="code" :value="code" /></el-select>
      </div></header>
      <div v-if="product" class="product-hover-facts" :class="{ visible: productHoverFacts }" data-test="product-member-hover-facts" :data-member-key="productHoverFacts?.key || ''">
        <template v-if="productHoverFacts"><strong>{{ productHoverFacts.name }}</strong><span class="hover-direction" :class="productHover.direction === 'positive' ? 'long' : 'short'">{{ productHoverDirection }}</span><span>多头 {{ formatPublished(productHoverFacts.long) }} 张</span><span>空头 {{ formatPublished(productHoverFacts.short) }} 张</span><span>净多 {{ formatPublished(productHoverFacts.netLong) }} 张</span><span>净空 {{ formatPublished(productHoverFacts.netShort) }} 张</span></template>
      </div>
      <div v-if="product" class="product-pair-rows">
        <div class="product-pair-row" data-test="product-gross-row"><FuturesSignedBarChart :rows="productGrossRows" :loading="loading" :summary="productGrossSummary" :extent="productGrossExtent" :selected-key="productMemberKey" :linked-hover-key="productHover.source === 'net' ? productHover.key : ''" shared-hover :title="`${selectedProductName}的席位多空持仓`" positive-label="多头持仓" negative-label="空头持仓" @select="productMemberKey = $event" @hover="updateProductHover('gross',$event)"><template #controls><FuturesTradingDatePicker :dates="productDates" :model-value="playbackDay" label="期货席位多空日期" @open="pausePlayback" @update:model-value="selectPlaybackDay" /></template></FuturesSignedBarChart><FuturesPositionSpreadAreaChart :loading="loading" :dates="productDates" :long-values="productHistoryValues('longPosition')" :short-values="productHistoryValues('shortPosition')" :title="productMemberKey ? `${selectedProductMemberName} · 多空持仓历史` : '全部已公布席位多空持仓历史'" long-label="多头持仓" short-label="空头持仓" /></div>
        <div class="product-pair-row" data-test="product-net-row"><FuturesSignedBarChart :rows="productNetRows" :loading="loading" :summary="productNetSummary" :extent="productNetExtent" :selected-key="productMemberKey" :linked-hover-key="productHover.source === 'gross' ? productHover.key : ''" shared-hover title="席位净多与净空" positive-label="净多持仓" negative-label="净空持仓" @select="productMemberKey = $event" @hover="updateProductHover('net',$event)"><template #controls><FuturesTradingDatePicker :dates="productDates" :model-value="playbackDay" label="期货席位净仓日期" @open="pausePlayback" @update:model-value="selectPlaybackDay" /></template></FuturesSignedBarChart><FuturesPositionSpreadAreaChart :loading="loading" :dates="productDates" :long-values="productHistoryValues('netLongPosition')" :short-values="productHistoryValues('netShortPosition')" :title="productMemberKey ? `${selectedProductMemberName} · 净多净空历史` : '全部已公布席位净多净空历史'" long-label="净多持仓" short-label="净空持仓" /></div>
      </div>
      <div v-else-if="loading" class="product-pair-rows"><div v-for="row in 2" :key="row" class="product-pair-row"><div class="skeleton-panel"><FuturesLoadingSkeleton kind="ranking" /></div><div class="skeleton-panel"><FuturesLoadingSkeleton kind="overview" /></div></div></div>
      <div v-else class="loading-chart">暂无已公布席位</div>
    </section>

    <section id="futures-contracts" class="contract-section"><FuturesContractInfo v-if="contractInfo" :payload="contractInfo" />
    <div v-else-if="loading" class="panel"><FuturesLoadingSkeleton kind="ranking" /></div>
    <div v-else class="panel loading-chart">官网期货合约信息暂不可用</div></section>

    <section id="futures-questions" class="panel questions" data-test="futures-qa">
      <h2>常见问题</h2>
      <details><summary>多空热度如何计算？</summary>
        <p>只使用六个发布场所已核验的普通与月均价期货。数量和涨跌幅根据每个交易日的今结算相对昨结算计算；连续周采用截至当日已经结束的交易周。总体热度每日由连续周 60%、合约数量 20%、加权涨跌幅 20% 合成；成交额不参与热度评分，也不在热度模块显示。连续周同向不足两周时加持为0；第二周起额外方向票依次为1、3、7，总票数逐周翻番，不设人为封顶。所选品种热度只表示该品种合约。</p>
        <p>历史折线显示每日总热度及三个分项的热度分数；上方卡片只显示最新交易日的上涨、下跌及平盘合约数量，至少连涨或连跌两周的合约数量，以及正负持仓加权结算变化。连续周分项在没有新完成交易周的日子可能保持不变，其余分项按当日事实变化。成交额继续用于下方品种气泡的横轴。</p>
        <p>版本：{{ weeklyHeat?.formulaVersion || '暂无' }}；最新交易日：{{ weeklyHeat?.latest?.tradeDate || '暂无' }}；质量：{{ weeklyHeat?.latest?.dataQualityStatus || '暂无' }}；范围内部分或不可用日：{{ weeklyHeat?.points?.filter(point => point.dataQualityStatus !== 'PASS').length || 0 }}。<span v-if="weeklyHeat?.latest"> 最新日缺有效结算的合约 {{ weeklyHeat.latest.coverage?.invalidContracts?.length || 0 }} 个，待补周：{{ weeklyHeat.latest.pendingWeek || '无' }}。</span></p>
      </details>
      <details><summary>气泡的大小和位置表示什么？</summary>
        <p>每颗气泡代表一个“交易所＋品种”，逐日汇总该品种全部有效月份合约。横轴是官网单边成交额合计（亿元）：以当前范围内最近有数据的交易日为参照，按已核验品种的成交额分位展开，低成交额区域更宽；刻度显示真实金额。同额品种保持同一横坐标，缺报及沿用位置不参与分位统计，因此各区间数量不一定完全相等。播放期间固定参照，历史日期的相同金额也在同一位置。参照样本过少时使用线性刻度。纵轴是最近最多60个实际交易日的品种加权日涨跌幅直接相加（百分点），气泡大小随逐月沉淀资金合计变化。可视直径采用对数刻度突出小额差异，并保留最小可见尺寸；精确金额以悬浮数值为准。逐月资金按单边持仓量 × 结算价 × 交易乘数 ×（官方投机多头保证金率＋投机空头保证金率）估算；任何有效月份缺必要字段，品种资金合计保持缺报，不用部分合约之和冒充全部。</p>
        <p>每日方向按各月份合约今结算相对昨结算的变化，以当日单边持仓量加权；窗口中的每日百分数直接相加，单位为百分点，不是复利收益或可交易收益。悬浮显示上涨、下跌、平盘的天数和实际窗口长度。新日进入窗口时最早一日也会退出，因而当天上涨时累计值也可能回退。存在未核验交易日、缺失日或无有效持仓时累计缺报，不补零。</p>
        <p>红圈↑和绿圈↓标示近3个已核验交易日的品种日涨跌幅直接和在当日有效品种中分别位于上涨前10%或下跌前10%；至少20个有效品种且三日交易所来源完整才进行比较，并列使用相同名次。该标记描述短期相对强弱，不代表趋势已确认或未来收益。盘后信息仅以所选日及以前的数据计算；历史源数据可能后来修订。</p>
        <p>不同月份的持仓权重会随时间变化，因此加权结算价的首尾变化可能包含合约结构变化；它用于描述品种，不代表某个可交易合约的收益。10个交易日的结算价首尾之间有9个相邻交易日间隔。</p>
        <p>中金所按所选交易日之前最近一次完整官网结算参数公布表取各合约保证金率，直到下一次公布；悬浮显示实际参数公布日。若最新表没有列出某个有持仓月份，该月份仍缺报，不借用更早表。其他交易所沿用各自的同日有效参数门。</p>
        <p>加权价格每天由全部有效月份合约的单边持仓量加权结算价形成，换月会改变序列构成，不能将合成涨跌幅读作单个合约收益。新上市品种仅在精确官网上市日证据通过后采用不足10日的连续短窗口；只有1个交易日时以当天为基准显示0%。窗口内任一交易日缺有效加权结算价，收益保持空值。</p>
        <p>品种首次有已发布指标后持续显示。实心表示成交额、方向累计和沉淀资金均有值；空心表示至少一项缺报，即使成交额和资金均为真实零、方向缺报时仍为空心。缺值时仅为展示沿用该指标最近可用的位置或大小，尚无可沿用值时暂置零轴或画最小空心圆，均不代表真实零值。悬浮可查看当日原值、缺报原因及沿用日期，右侧历史线保留缺口。</p>
        <p>颜色按品种稳定分配。右侧显示所选品种的成交额、近1～10交易日涨跌幅和沉淀资金三条历史线，以及单独的方向累计历史；未选品种时，两项金额采用官网完整日全部品种合计，涨跌幅与方向累计按可核验的正市值品种等权显示。页面底部播放器按同一实际交易日同步市场气泡和席位快照，过渡帧仅作视觉插值。坐标区和轴支持拖动平移、滚轮缩放和双击复位。</p>
        <p>成交额公式：{{ distributions.turnover?.formulaVersion || '暂无' }}；十日涨跌公式：{{ distributions.return?.formulaVersion || '暂无' }}；方向累计公式：{{ distributions.return?.directionFormulaVersion || '暂无' }}；沉淀资金公式：{{ distributions.deposit?.formulaVersion || '暂无' }}。</p>
      </details>
      <details><summary>席位持仓范围和日期播放是什么口径？</summary><p>席位只统计交易所已公布排名。公司名末尾“（代客）”归并，多头向右、空头向左显示。席位持仓分布可选单个期货公司或全部席位，按品种展示多空、净多、净空三张柱图；全部席位按各公司已公布持仓汇总，净仓仍先在同合约同公司双侧配对，再跨公司与合约求和。所选品种的多空柱图与历史面积图同列成一行，净仓柱图与净仓历史面积图另成一行；各柱图纵轴列出期货公司。每张柱图只显示所选日期至少有一侧非零持仓的公司或品种；两侧均为0或未公布的行隐藏。每张图窗显示10行，竖向滚动可查看其余名单。两张品种席位柱图顶部的“全部席位”汇总柱只累加交易所已公布的席位。全部值缺失的日期保持空值，缺单侧不补零。悬停多头或空头一侧时，同一行两侧柱同时突出，固定信息栏显示多、空、净多、净空四项；点击柱组切换所选历史。日期滑块可选近20交易日、近60交易日、近半年交易日或近3年交易日，默认0.5×且可选0.5×、1×、2×、4×；鼠标悬停不会暂停，拖动会暂停。缺失排名日清空快照，不延用上一日数值；历史面积图按两侧原始持仓差填红绿，缺失日留空。</p><p>排名基准日：{{ directory?.baselineDay || '暂无' }}。</p></details>
      <details><summary>日历的黄色圆点表示什么？</summary><p>黄色圆点标出当前历史范围内的实际交易日，休市日不可选择。三处日历与底部播放器共用同一日期，点击日历入口会暂停播放，选日后所有快照同步；历史折线仍显示完整所选范围。交易日可能有个别席位未公布持仓，此时对应柱图留空。</p></details>
      <details><summary>为什么两页同合约的市值可能不同？</summary><p>本页的期货统计按交易所官网逐合约结算价和单边持仓量计算，保留2023年以来的官网历史。全部行情页按通达信期货通盘后下载的结算价和持仓量计算，官网提供成交额、生效乘数及投机保证金率。两套量价仓若有真实差异，全部行情页的相关派生值进入待核验状态；来源更新后一致才恢复。</p></details>
      <details><summary>期货合约信息的上市日期是什么？</summary><p>上市交易日是期货品种首次允许交易的交易日，不是具体月份合约的挂牌日，也不是官网合约文本的发布日期。只有取得明确的交易所上市通知或官方历史资料才显示已核验日期；本项目官网日行情的首见日期单独列出，不能代替历史上市日期。合约文本最低保证金和涨跌停板规则也不等同于所选交易日实际结算参数。未取得的条款显示“—”，不会把校验页当作合约文本。</p></details>
    </section>
    <aside ref="playbackDock" class="playback-dock" data-test="futures-playback-dock" aria-label="期货数据日期播放">
      <div class="playback-dock-inner">
        <div class="dock-controls"><FuturesBubbleTimeline v-if="playbackDates.length" v-model="playbackPosition" v-model:window="playbackWindow" :dates="playbackDates" :reset-key="playbackReset" /><FuturesLoadingSkeleton v-else-if="loading" kind="timeline" /><span v-else class="dock-empty">暂无可播放日期</span></div>
      </div>
    </aside>
  </main>
</template>

<style scoped>
.futures-page { display: grid; grid-template-columns: minmax(0,1fr); gap: 20px; min-width: 0; padding-bottom: 100px; color: var(--ml-text-primary); }
.futures-page > * { min-width: 0; }
.futures-page > section { scroll-margin-top: 76px; }
.futures-status-notice.el-alert--info { background: color-mix(in srgb,var(--ml-accent) 5%,var(--ml-surface)); color: var(--ml-text-secondary); border: 1px solid color-mix(in srgb,var(--ml-accent) 18%,var(--ml-divider)); border-radius: 8px; padding: 10px 14px; }
.futures-status-notice :deep(.el-alert__title) { color: var(--ml-text-secondary); font-size: 12px; line-height: 1.6; }
.futures-status-notice :deep(.el-alert__icon) { color: var(--ml-accent); font-size: 15px; }
.futures-error-notice.el-alert--error { background: color-mix(in srgb,var(--el-color-danger) 8%,var(--ml-surface)); color: var(--ml-text-primary); border: 1px solid color-mix(in srgb,var(--el-color-danger) 32%,var(--ml-divider)); border-radius: 8px; }
.futures-error-notice :deep(.el-alert__title) { color: var(--ml-text-primary); font-size: 12px; overflow-wrap: anywhere; }
.futures-update-summary { display: flex; align-items: center; gap: 8px 16px; flex-wrap: wrap; padding: 10px 14px; border: 1px solid var(--ml-divider); border-radius: 9px; background: var(--ml-surface); font-size: 12px; line-height: 1.6; }
.futures-update-summary > span { font-weight: 600; }
.futures-update-summary small { flex: 1 1 420px; color: var(--ml-text-secondary); font-size: 11px; }
.futures-update-summary button { border: 1px solid var(--ml-divider); border-radius: 7px; padding: 5px 10px; background: var(--ml-surface); color: var(--ml-accent); font: inherit; font-weight: 600; cursor: pointer; }
.futures-update-summary button:disabled { opacity: .55; cursor: wait; }
.futures-update-summary button:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
.update-source-status { display: flex; flex-wrap: wrap; gap: 6px 12px; width: 100%; color: var(--ml-text-secondary); font-size: 11px; }
.page-heading, section > header, .controls, .bubble-tools { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.page-heading { padding-block: 2px; flex-direction: row; }
.page-heading :deep(.el-button) { min-height: 34px; border-radius: 8px; font-size: 12px; font-weight: 600; }
.refresh-icon { width: 15px; height: 15px; margin-right: 7px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.section-nav { position: sticky; top: 0; z-index: 12; display: flex; align-items: center; gap: 4px; min-width: 0; margin-top: -8px; padding: 5px; overflow-x: auto; border: 1px solid var(--ml-divider); border-radius: 11px; background: var(--ml-surface); scrollbar-width: none; }
.section-nav::-webkit-scrollbar { display: none; }
.section-nav button { position: relative; flex: none; min-height: 34px; padding: 7px 16px; border: 0; border-radius: 7px; background: transparent; color: var(--ml-text-secondary); font-size: 12px; font-weight: 600; cursor: pointer; transition: color 140ms, background-color 140ms; }
.section-nav button:hover { color: var(--ml-text-primary); background: var(--ml-surface-selected); }
.section-nav button.active { color: var(--ml-accent); background: var(--ml-accent-soft); }
.section-nav button:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: -2px; }
h1,h2 { margin: 0; } h2 { font-size: 18px; }
.panel { padding: 20px; min-width: 0; border: 1px solid var(--ml-divider); border-radius: 12px; background: var(--ml-surface); }
.panel > header { min-height: 36px; }
.panel > header h2,.heat-section :deep(.weekly-heat h2),.contract-section :deep(.contract-info h2) { font-size: 16px; font-weight: 650; letter-spacing: .01em; }
.seat-section > header { margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--ml-divider); }
.page-heading h1 { font-size: 22px; font-weight: 700; letter-spacing: .02em; }
.controls { justify-content: flex-end; }
.section-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; min-width: 0; }
.selection-context { display: inline-flex; align-items: center; gap: 5px; max-width: min(100%,320px); padding: 4px 7px 4px 10px; border: 1px solid color-mix(in srgb,var(--ml-accent) 16%,var(--ml-divider)); border-radius: 7px; color: var(--ml-accent); background: var(--ml-accent-soft); font-size: 12px; line-height: 20px; }
.selection-context > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.selection-context button { flex: none; display: grid; place-items: center; width: 24px; height: 24px; padding: 4px; border: 0; border-radius: 4px; color: inherit; background: transparent; cursor: pointer; }
.selection-context button:hover { background: color-mix(in srgb,var(--ml-accent) 12%,transparent); }
.selection-context button:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
.selection-context svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; }
.controls :deep(.el-select__wrapper), .heat-section :deep(.el-select__wrapper), .bubble-tools :deep(.el-select__wrapper), .bubble-tools :deep(.el-input__wrapper) { border-radius: 8px; min-height: 34px; background: var(--ml-surface-elevated); }
.market-distributions { display: grid; gap: 12px; }
.playback-dock { position: fixed; inset-inline: 0; bottom: 0; z-index: 60; border-top: 1px solid var(--ml-divider); background: color-mix(in srgb, var(--ml-surface) 96%, transparent); box-shadow: 0 -4px 18px rgba(15,25,45,.06); backdrop-filter: blur(14px); }
.playback-dock-inner { max-width: 1720px; margin: 0 auto; padding: 7px 28px calc(7px + env(safe-area-inset-bottom)); }
.dock-controls { min-height: 48px; }
.dock-empty { display: grid; min-height: 48px; place-items: center; color: var(--ml-text-secondary); font-size: 12px; }
.dock-controls :deep(.bubble-timeline) { margin: 0; padding: 0; border: 0; border-radius: 0; background: transparent; box-shadow: none; }
.dock-controls :deep(.futures-skeleton.is-timeline) { padding: 0; }
.dock-controls :deep(.futures-skeleton.is-timeline .sk-heading),.dock-controls :deep(.futures-skeleton.is-timeline .sk-ticks) { display: none; }
.dock-controls :deep(.futures-skeleton.is-timeline .sk-track) { margin: 18px 4px; }
.member-visuals,.member-net-visuals,.product-pair-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: stretch; }
.member-visuals { margin-top: 0; }
.member-net-visuals { margin-top: 16px; }
.product-pair-rows { display: grid; gap: 16px; margin-top: 10px; }
.member-visuals :deep(.signed-bar-panel),.member-net-visuals :deep(.signed-bar-panel),.product-pair-row :deep(.signed-bar-panel),.member-visuals :deep(.spread-chart),.product-pair-row :deep(.spread-chart) { border-radius: 10px; background: var(--ml-surface-elevated); box-shadow: none; }
.member-visuals :deep(.signed-bar-panel:hover),.member-net-visuals :deep(.signed-bar-panel:hover),.product-pair-row :deep(.signed-bar-panel:hover),.member-visuals :deep(.spread-chart:hover),.product-pair-row :deep(.spread-chart:hover) { border-color: color-mix(in srgb,var(--ml-accent) 18%,var(--ml-divider)); box-shadow: none; }
.member-visuals :deep(.signed-bar-panel:focus-within),.member-net-visuals :deep(.signed-bar-panel:focus-within),.product-pair-row :deep(.signed-bar-panel:focus-within) { border-color: color-mix(in srgb,var(--ml-accent) 45%,var(--ml-divider)); }
.member-visuals :deep(h3),.member-net-visuals :deep(h3),.product-pair-row :deep(h3) { font-size: 13px; font-weight: 600; line-height: 1.6; }
.member-visuals :deep(.spread-chart),.product-pair-row :deep(.spread-chart) { margin-top: 0; }
.member-visuals :deep(.spread-chart .chart) { height: 540px; }
.product-pair-row :deep(.spread-chart .chart) { height: 600px; }
.skeleton-panel { min-width: 0; padding: 12px; border: 1px solid var(--ml-divider); border-radius: 10px; background: var(--ml-surface-elevated); }
.product-hover-facts { min-height: 34px; display: flex; align-items: center; flex-wrap: wrap; gap: 5px 13px; margin: 8px 0; padding: 5px 10px; border: 1px solid transparent; border-radius: 9px; color: var(--ml-text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; opacity: 0; transition: opacity 120ms; }
.product-hover-facts.visible { border-color: var(--ml-divider); background: var(--ml-surface-selected); opacity: 1; }
.product-hover-facts strong { color: var(--ml-text-primary); font-size: 12px; }
.product-hover-facts span:not(.hover-direction) { font-variant-numeric: tabular-nums; }
.hover-direction { padding: 2px 7px; border-radius: 5px; font-weight: 700; }
.hover-direction.long { color: var(--ml-price-up); background: color-mix(in srgb, var(--ml-price-up) 10%, transparent); }
.hover-direction.short { color: var(--ml-price-down); background: color-mix(in srgb, var(--ml-price-down) 10%, transparent); }
.distribution-card { margin: 0; background: var(--ml-surface); }
.distribution-card > header h2 { display: flex; align-items: center; gap: 9px; }
.distribution-card > header h2::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--ml-accent); }
.bubble-tools { display: grid; grid-template-columns: minmax(220px, 300px) minmax(180px,260px); justify-content: end; margin: 12px 0 16px; padding-bottom: 14px; border-bottom: 1px solid var(--ml-divider); }
.bubble-legend { display: flex; flex-wrap: wrap; gap: 8px 20px; margin: -4px 0 12px; color: var(--ml-text-secondary); font-size: 12px; }
.bubble-legend span { display: inline-flex; align-items: center; gap: 6px; }
.bubble-legend-dot { display: inline-block; width: 10px; height: 10px; flex: none; border: 2px solid var(--ml-accent); border-radius: 50%; }
.bubble-legend-dot.solid { background: var(--ml-accent); }
.bubble-legend-dot.hollow { background: transparent; }
.bubble-source-gap,.bubble-margin-gap { margin: -2px 0 12px; padding: 9px 12px; border: 1px solid color-mix(in srgb, #d89232 42%, var(--ml-divider)); border-radius: 8px; background: color-mix(in srgb, #d89232 8%, var(--ml-surface)); color: var(--ml-text-primary); font-size: 12px; line-height: 1.6; }
.bubble-source-gap p,.bubble-margin-gap p { margin: 0; }
.bubble-tools .el-select, .bubble-tools .el-input { width: 100%; }
.distribution-visuals { display: grid; grid-template-columns: minmax(0,1.35fr) minmax(360px,1fr); gap: 16px; align-items: stretch; }
.distribution-visuals > * { min-width: 0; }
.distribution-visuals :deep(.futures-skeleton.is-overview) { height: 100%; }
.el-select,.el-input { width: 250px; max-width: 100%; }
.loading-chart,.empty-panel { min-height: 100px; display: grid; place-items: center; color: var(--ml-text-secondary); }
.questions h2 { margin-bottom: 8px; font-size: 16px; }
details { padding: 0; border-bottom: 1px solid var(--ml-divider); }
details:last-child { border-bottom: 0; }
summary { padding: 15px 4px; cursor: pointer; font-size: 13px; font-weight: 600; transition: color 160ms; }summary:hover,details[open] summary { color: var(--ml-accent); }
summary:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; border-radius: 5px; }
.questions p { color: var(--ml-text-secondary); line-height: 1.8; font-size: 13px; }
.questions details[open] { padding-bottom: 6px; }
.questions details[open] p { margin-top: 0; padding-inline: 4px; }
@media (max-width: 900px) { .member-visuals,.member-net-visuals,.product-pair-row { grid-template-columns: 1fr; }.member-visuals :deep(.spread-chart .chart),.product-pair-row :deep(.spread-chart .chart) { height: 330px; } }
@media (max-width: 1100px) { .distribution-visuals { grid-template-columns: minmax(0,1fr); }.distribution-visuals :deep(.triple-metric-card) { min-height: 440px; } }
@media (max-width: 900px) { .futures-page { padding-bottom: 120px; }.playback-dock-inner { padding: 5px 16px; }.controls { gap: 8px; }.section-nav button { padding-inline: 12px; } }
@media (max-width: 600px) { .panel { padding: 14px; }.page-heading h1 { font-size: 20px; }.bubble-tools { grid-template-columns: 1fr; gap: 8px; }.seat-section > header { align-items: flex-start; }.seat-section .controls { width: 100%; justify-content: flex-start; display: grid; grid-template-columns: minmax(0,1fr) auto; }.seat-section .controls > .el-select { width: 100%; }.seat-section .controls > .el-select:only-child { grid-column: 1 / -1; }.section-nav { margin-inline: -2px; }.section-nav button { padding-inline: 10px; font-size: 11px; }.section-heading { gap: 8px; }.selection-context { max-width: 200px; } }
@media (max-width: 600px) { .seat-section .controls.product-filters { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 380px) { .seat-section .controls,.seat-section .controls.product-filters { grid-template-columns: minmax(0,1fr); }.seat-section .controls > .el-select { grid-column: 1; } }
@media (max-width: 500px) { .futures-page { padding-bottom: 150px; } }
.futures-page { padding-bottom: calc(var(--futures-dock-clearance,76px) + 24px); }
@media (prefers-reduced-motion: reduce) { .panel, summary,.section-nav button { transition: none; } }
</style>
