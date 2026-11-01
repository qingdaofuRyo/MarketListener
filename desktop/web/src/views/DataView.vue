<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import CompositionDonutChart from "../components/charts/CompositionDonutChart.vue";
import ExchangeMarketStats, { type ExchangeOverview } from "../components/charts/ExchangeMarketStats.vue";
import SeriesChart, { type NamedSeries } from "../components/charts/SeriesChart.vue";
import SparklineChart from "../components/charts/SparklineChart.vue";
import ResearchLoadingSkeleton from "./ResearchLoadingSkeleton.vue";
import { apiGet, apiPost, invalidateQuery } from "../domain/api";
import { useThemeStore } from "../stores/theme";

type Market = "cn" | "hk";
type NumericField = { [K in keyof EquityPoint]-?: Exclude<EquityPoint[K], undefined> extends number | null ? K : never }[keyof EquityPoint];
interface PointSource { label: string; coverage?: number; pairedCoverage?: number }
interface SectionMetric {
  metricKey: string;
  tradingDate: string;
  metricName: string;
  value: number;
  source?: string;
  unit?: string;
  coverage?: number | { poolCount: number; pricedCount: number };
}
interface SectionPanel {
  id: string;
  title: string;
  status: "PASS" | "PARTIAL" | "UNAVAILABLE";
  values?: SectionMetric[];
  limitations?: string[];
  stale?: boolean;
}
interface SectionPayload { id: string; title: string; panels: SectionPanel[] }
interface EquityPoint {
  tradingDay: string;
  totalMarketCapYi: number | null;
  floatMarketCapYi?: number | null;
  turnoverYi: number | null;
  advances?: number | null;
  unchanged?: number | null;
  declines?: number | null;
  limitUpCount?: number | null;
  limitDownCount?: number | null;
  failedLimitUpCount?: number | null;
  sealedLimitUpRatePct?: number | null;
  limitUpHeight?: number | null;
  limitDownHeight?: number | null;
  yesterdayLimitUpOpenReturnPct?: number | null;
  coverage?: number;
  turnoverCoverage?: number | null;
  referenceCoverage?: number | null;
  coverageStatus?: string;
  sources?: Record<string, PointSource>;
}
interface EquityOverview {
  available: boolean;
  market: "CN" | "HK";
  points: EquityPoint[];
  limitations: string[];
}
interface EconomyItem {
  id: string;
  name: string;
  group: string;
  topic: string;
  latestDay: string | null;
  latestValue: number | null;
  unit: string;
  source: string;
  available: boolean;
}
interface EconomySeries {
  id: string;
  name: string;
  group: string;
  topic: string;
  unit: string;
  source: string;
  points: Array<{ day: string; value: number | null }>;
  available: boolean;
  limitations: string[];
}
interface EconomyPreview { id: string; points: Array<{ day: string; value: number | null }> }
interface THSCPI { available: boolean; source: string; fetchedAt?: string | null;
  points: Array<{ month: string; index: number | null; yoyPct: number | null; momPct: number | null }>;
  limitations: string[] }
interface BlsPayrollSeries { available: boolean; observations: Array<{ observationPeriod: string; value: number; fetchedAt: string }> }
interface THSIndexFlashStatus { status: "NEVER" | "BLOCKED" | "FAILED" | "UNVERIFIED"; checkedAt: string | null }
interface ThsEtfObservation { available: boolean; status: string; scope?: string; count: number;
  captureStart?: string; captureEnd?: string; quoteTime?: string | null;
  rows: Array<{ code: string; name: string; price: number | null; changePct: number | null;
    turnover: number | null; observedAt: string }> }
type EtfSortField = "price" | "changePct" | "turnover";
interface ThsEtfUpdateStatus { status: string; autoEnabled: boolean; intervalMinutes: number;
  attemptAt?: string | null; lastSuccessAt?: string | null; count?: number | null }
interface KplMarketStatus { status: string; checkedAt: string | null; observedDate: string | null;
  calendarStatus: "VERIFIED" | "UNKNOWN"; checkAge: "RECENT" | "OLD" | "UNKNOWN" }
interface KplPoint { day: string; limitUp: number | null; limitDown: number | null;
  naturalLimitUp: number | null; everLimitDown: number | null; brokenCount: number | null; brokenRatePct: number | null }
interface KplHistory { available: boolean; fetchedAt: string | null; latestObservedAt?: string; latestDay?: string;
  latestPhase?: "INTRADAY" | "AFTER_CLOSE" | "HISTORICAL"; calendarIncomplete: boolean; points: KplPoint[] }
interface DisclosureHit { announcementId: string; securityCode: string; securityName: string;
  title: string; publicationDay: string; publishedAt: string; pdfUrl: string;
  keyword: string; matchStatus: string }
interface DisclosureWatch { available: boolean; items: DisclosureHit[]; total: number;
  keywords: string[]; riskSignalTerms: string[]; lastFetchedAt?: string | null; limitations: string[] }
interface SearchInterestSources { terms: string[]; series: unknown[];
  sources: Array<{ id: string; name: string; status: string; reason: string; url: string }> }
interface RiskSignalCategories { status: string; categories: Array<{ id: string; name: string;
  terms: string[]; requiredEvidence: string }>; ruleSources: Array<{ board: string; url: string }>;
  limitations: string[] }
interface DataUpdateStatus { autoEnabled: boolean; completedRevision: string;
  jobs: Array<{ kind: string; status: string; completedAt: string | null }> }
interface ChartSpec {
  series: NamedSeries[];
  unit: string;
  type: "line" | "bar";
  area: boolean;
  colors: string[];
}
interface CompositionItem { name: string; value: number; color: string }
interface SourceTile { id: string; name: string; observation: string; detail: string;
  status: string; tone: "ready" | "partial" | "periodic" | "missing"; href: string }

const route = useRoute();
const theme = useThemeStore();
const sections = ref<SectionPayload[]>([]);
const sectionsLoading = ref(true);
const sectionsError = ref("");
const overviews = ref<Partial<Record<Market, EquityOverview>>>({});
const overviewLoading = ref<Record<Market, boolean>>({ cn: true, hk: true });
const overviewErrors = ref<Record<Market, string>>({ cn: "", hk: "" });
const exchangeOverview = ref<ExchangeOverview>();
const exchangeLoading = ref(true);
const exchangeError = ref("");
const economyCatalog = ref<EconomyItem[]>([]);
const economyCatalogLoading = ref(true);
const economySeries = ref<EconomySeries>();
const economySeriesLoading = ref(false);
const economyError = ref("");
const selectedEconomyId = ref("");
const economySearch = ref("");
const miniSeries = ref<Record<string, EconomyPreview["points"]>>({});
const refreshBusy = ref(false);
const thsCPI = ref<THSCPI>();
const blsPayroll = ref<BlsPayrollSeries>();
const blsPayrollError = ref(false);
const thsFlash = ref<THSIndexFlashStatus>();
const thsEtf = ref<ThsEtfObservation>();
const thsEtfError = ref(false);
const thsEtfUpdate = ref<ThsEtfUpdateStatus>();
const thsEtfSearch = ref("");
const etfSortField = ref<EtfSortField | null>(null);
const etfSortDirection = ref<"ascending" | "descending">("descending");
const thsEtfRows = computed(() => {
  const term = thsEtfSearch.value.trim().toLowerCase();
  const rows = (thsEtf.value?.rows || []).filter(row => !term || `${row.code} ${row.name}`.toLowerCase().includes(term));
  const field = etfSortField.value;
  if (!field) return rows;
  const direction = etfSortDirection.value === "ascending" ? 1 : -1;
  return rows.map((row, index) => ({ row, index })).sort((a, b) => {
    const av = a.row[field], bv = b.row[field];
    if (av === null || bv === null) return av === bv ? a.index - b.index : av === null ? 1 : -1;
    return (av - bv) * direction || a.index - b.index;
  }).map(item => item.row);
});
function sortEtf(field: EtfSortField): void {
  etfSortDirection.value = etfSortField.value === field && etfSortDirection.value === "descending" ? "ascending" : "descending";
  etfSortField.value = field;
}
function etfSortState(field: EtfSortField): "ascending" | "descending" | "none" {
  return etfSortField.value === field ? etfSortDirection.value : "none";
}
function etfTime(value: string): string {
  return new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
const kplMarket = ref<KplMarketStatus>();
const kplHistory = ref<KplHistory>();
const kplReadError = ref(false);
const kplCounts = computed<NamedSeries[]>(() => ([
  ["涨停", "limitUp"], ["跌停", "limitDown"], ["自然涨停", "naturalLimitUp"],
  ["曾跌停", "everLimitDown"], ["炸板", "brokenCount"],
] as const).map(([name, field]) => ({ name, points: (kplHistory.value?.points || []).map(point => ({ t: point.day, value: point[field] })) })));
const kplRate = computed<NamedSeries[]>(() => [{ name: "破板率", points: (kplHistory.value?.points || []).map(point => ({ t: point.day, value: point.brokenRatePct })) }]);
const disclosureWatch = ref<DisclosureWatch>();
const disclosureKeyword = ref("");
const searchInterest = ref<SearchInterestSources>();
const riskCategories = ref<RiskSignalCategories>();
const dataUpdateStatus = ref<DataUpdateStatus>();
const dataUpdateStatusError = ref(false);
const auxiliaryError = ref("");
const auxiliaryLoading = ref(true);
const requestingSourceUpdate = ref(false);
let dataStatusTimer: number | undefined;
let updateStatusRequest = 0;
let thsEtfRequest = 0;
let thsEtfStatusRequest = 0;
let kplRequest = 0;
let sectionsRequest = 0;
let catalogRequest = 0;
let seriesRequest = 0;
let previewsRequest = 0;
const overviewRequest: Record<Market, number> = { cn: 0, hk: 0 };
let exchangeRequest = 0;

const marketSections = [
  { market: "cn", id: "data-cn", label: "A股", description: "市值和成交额取沪深京同日官网统计；涨跌停及接盘收益按收盘核验，休市显示最近交易日。" },
  { market: "hk", id: "data-hk", label: "港股", description: "市值为已保存的港交所档案，自动网页采集暂停；成交额与涨跌家数为本机股票日线样本。" },
] as const;
const marketPanelIds: Record<Market, string[]> = {
  cn: ["cn-market-cap", "cn-float-market-cap", "cn-turnover", "cn-breadth", "cn-limit-pool", "cn-broken-board", "cn-sealed-rate", "cn-limit-height", "cn-limit-up-open-return"],
  hk: ["hk-market-cap", "hk-turnover", "hk-breadth"],
};
const featuredIds = ["TDX_MACRO.3_PMI", "TDX_MACRO.2_CPI", "TDX_MACRO.8_NONAG", "TDX_MACRO.5_M2", "TDX_MACRO.8_EFFR"];
const ratioSpecs = [
  { id: "GOLD_SILVER_RATIO", name: "金银比" },
  { id: "GOLD_COPPER_RATIO", name: "金铜比" },
  { id: "GOLD_OIL_RATIO", name: "金油比" },
];
const panelsByMarket = computed<Record<Market, SectionPanel[]>>(() => {
  const result = { cn: [] as SectionPanel[], hk: [] as SectionPanel[] };
  for (const market of ["cn", "hk"] as const) {
    const sectionId = market === "cn" ? "cn-equities" : "hk-equities";
    const available = sections.value.find(item => item.id === sectionId)?.panels || [];
    result[market] = marketPanelIds[market]
      .map(id => available.find(panel => panel.id === id))
      .filter((item): item is SectionPanel => Boolean(item));
  }
  return result;
});
const catalogById = computed(() => new Map(economyCatalog.value.map(item => [item.id, item])));
const featuredItems = computed(() => featuredIds.map(id => catalogById.value.get(id)).filter((item): item is EconomyItem => Boolean(item)));
const ratioItems = computed<EconomyItem[]>(() => ratioSpecs.map(spec => catalogById.value.get(spec.id) || {
  ...spec, group: "ratio", topic: "商品比价", latestDay: null, latestValue: null,
  unit: "", source: "", available: false,
}));
const selectedEconomy = computed(() => catalogById.value.get(selectedEconomyId.value));
const selectedTimeline = computed<NamedSeries[]>(() => economySeries.value?.available
  ? [{ name: economySeries.value.name, points: economySeries.value.points.map(point => ({ t: point.day, value: point.value })) }]
  : []);
const cpiTimeline = computed<NamedSeries[]>(() => thsCPI.value?.available
  ? [{ name: "全国CPI当月指数", points: thsCPI.value.points.map(item => ({ t: item.month, value: item.index })) }]
  : []);
const latestThsCPI = computed(() => thsCPI.value?.points.at(-1));
const blsPayrollTimeline = computed<NamedSeries[]>(() => blsPayroll.value?.available
  ? [{ name: "BLS 非农就业变化", points: blsPayroll.value.observations.map(item => ({ t: item.observationPeriod, value: item.value })) }]
  : []);
const latestBlsPayroll = computed(() => blsPayroll.value?.observations.at(-1));
const economyGroups = computed(() => {
  const query = economySearch.value.trim().toLocaleLowerCase();
  const groups = new Map<string, EconomyItem[]>();
  for (const item of economyCatalog.value) {
    if (item.group !== "economy" || query && !(item.name + item.id).toLocaleLowerCase().includes(query)) continue;
    const entries = groups.get(item.topic) || [];
    entries.push(item);
    groups.set(item.topic, entries);
  }
  return [...groups.entries()].map(([topic, items]) => ({ topic, items }));
});
const sourceTiles = computed<SourceTile[]>(() => {
  const coverage = [...(exchangeOverview.value?.coverage || [])].reverse().find(item =>
    item.asset === "STOCK_A" && item.status !== "NOT_DUE");
  const stockDay = coverage?.day || null;
  const exchanges = ([
    ["SSE", "上交所", "上证主板A＋科创板"],
    ["SZSE", "深交所", "主板A＋创业板A"],
    ["BSE", "北交所", "北证股票日报"],
  ] as const).map(([venue, name, detail]) => {
    const series = exchangeOverview.value?.series.find(item => item.venue === venue &&
      item.category === "STOCK_A" && item.metric === "TOTAL_MARKET_CAP_YI");
    const latest = [...(series?.points || [])].reverse().find(point => typeof point.value === "number");
    const ready = Boolean(latest?.day === stockDay && coverage?.presentVenues.includes(venue));
    const loading = !latest && exchangeLoading.value;
    return { id: venue.toLowerCase(), name, observation: latest?.day || (loading ? "正在读取" : ""), detail,
      status: loading ? "读取中" : !latest ? "待接入" : ready ? "该日已核验" : "日期待补",
      tone: loading ? "periodic" : !latest ? "missing" : ready ? "ready" : "partial",
      href: "#data-exchange-stocks" } as SourceTile;
  });
  const pool = panelsByMarket.value.cn.find(item => item.id === "cn-limit-pool");
  const broken = panelsByMarket.value.cn.find(item => item.id === "cn-broken-board");
  const poolDates = (pool?.values || []).map(item => formatDate(item.tradingDate));
  const poolDay = poolDates.length ? poolDates.reduce((a, b) => a < b ? a : b) : "";
  const brokenDay = formatDate(broken?.values?.[0]?.tradingDate);
  const poolReady = Boolean(poolDay && poolDay === brokenDay && poolDay === stockDay);
  const hkCap = panelsByMarket.value.hk.find(item => item.id === "hk-market-cap")?.values?.[0];
  const hkPoints = overviews.value.hk?.points || [];
  const hkLatest = [...hkPoints].reverse().find(point => typeof point.turnoverYi === "number");
  const recentHkCutoff = hkPoints.length
    ? new Date(hkPoints[hkPoints.length - 1].tradingDay).getTime() - 90 * 86_400_000 : 0;
  const hkGaps = hkPoints.filter(point => point.coverageStatus === "INSUFFICIENT_LOCAL_FILES" &&
    new Date(point.tradingDay).getTime() >= recentHkCutoff).length;
  const economyCount = economyCatalog.value.filter(item => item.group === "economy" && item.available).length;
  return [
    ...exchanges,
    { id: "eastmoney", name: "东方财富涨跌停池", observation: poolDay || (sectionsLoading.value ? "正在读取" : ""),
      detail: "涨跌停、炸板与连板高度", status: !poolDay && sectionsLoading.value ? "读取中" : !poolDay ? "待补" : poolReady ? "该日已核验" : "分项待补",
      tone: !poolDay && sectionsLoading.value ? "periodic" : !poolDay ? "missing" : poolReady ? "ready" : "partial", href: "#data-cn" },
    { id: "hkex", name: "港交所市值档案", observation: hkCap ? formatDate(hkCap.tradingDate) : sectionsLoading.value ? "正在读取" : "",
      detail: "月度公布至上月底；自动网页采集暂停", status: !hkCap && sectionsLoading.value ? "读取中" : hkCap ? "已保存档案" : "暂无档案",
      tone: !hkCap && sectionsLoading.value ? "periodic" : hkCap ? "periodic" : "missing", href: "#data-hk" },
    { id: "tdx-hk", name: "通达信港股日线", observation: hkLatest?.tradingDay || (overviewLoading.value.hk ? "正在读取" : ""),
      detail: hkLatest ? `股票样本 ${hkLatest.turnoverCoverage ?? hkLatest.coverage ?? "—"} 只${hkGaps ? ` · 近90日 ${hkGaps} 日覆盖不足` : ""}` : "等待合格日线",
      status: !hkLatest && overviewLoading.value.hk ? "读取中" : hkLatest ? "股票样本" : "待补",
      tone: !hkLatest && overviewLoading.value.hk ? "periodic" : hkLatest ? "partial" : "missing", href: "#data-hk" },
    { id: "ths", name: "同花顺 CPI", observation: latestThsCPI.value?.month || (auxiliaryLoading.value ? "正在读取" : ""),
      detail: "月度观测；与通达信 CPI 分开", status: !latestThsCPI.value && auxiliaryLoading.value ? "读取中" : latestThsCPI.value ? "按月更新" : "待补",
      tone: !latestThsCPI.value && auxiliaryLoading.value ? "periodic" : latestThsCPI.value ? "periodic" : "missing", href: "#data-economy" },
    { id: "tdx-macro", name: "通达信经济序列", observation: economyCount ? `${economyCount} 项` : economyCatalogLoading.value ? "正在读取" : "",
      detail: "本机 38#；各指标观察频率不同", status: !economyCount && economyCatalogLoading.value ? "读取中" : economyCount ? "本机可用" : "待导入",
      tone: !economyCount && economyCatalogLoading.value ? "periodic" : economyCount ? "periodic" : "missing", href: "#data-economy" },
  ];
});
const hkCoverageGaps = computed(() => (overviews.value.hk?.points || [])
  .filter(point => point.coverageStatus === "INSUFFICIENT_LOCAL_FILES").slice(-3));

function formatDate(value?: string | null): string {
  if (!value) return "—";
  return /^\d{8}$/.test(value) ? value.replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3") : value.slice(0, 10);
}
function formatValue(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : "—";
}
function statusLabel(panel: SectionPanel): string {
  if (panel.status === "PASS") return "已核验";
  if (panel.status === "UNAVAILABLE") return "待补";
  if (panel.id === "hk-market-cap") return "月度档案";
  if (panel.id === "hk-turnover" || panel.id === "hk-breadth") return "股票样本";
  if (panel.id === "cn-breadth" || panel.id === "cn-limit-pool" ||
      panel.id === "cn-broken-board" || panel.id === "cn-sealed-rate" ||
      panel.id === "cn-limit-height" || panel.id === "cn-limit-up-open-return") return "限定口径";
  return "覆盖部分";
}
function metricLabel(metric: SectionMetric): string {
  const names: Record<string, string> = {
    ADVANCES: "上涨", UNCHANGED: "平盘", DECLINES: "下跌",
    "涨停_COUNT": "涨停", "跌停_COUNT": "跌停",
    "炸板_COUNT": "炸板",
    "涨停_MAX_LIANBAN": "涨停最高", "跌停_MAX_LIANBAN": "跌停最高",
    yesterdayLimitUpOpenReturnPct: "接盘收益",
  };
  return names[metric.metricKey] || metric.metricName.replace(/家数|当日|沪深京/gu, "");
}
function panelSource(panel: SectionPanel): string {
  if (panel.id === "cn-turnover" || panel.id === "cn-breadth") {
    const fields = panel.id === "cn-turnover" ? ["turnoverYi"] : ["advances", "unchanged", "declines"];
    const history = [...new Set((overviews.value.cn?.points || []).flatMap(point =>
      fields.map(field => point.sources?.[field]?.label).filter((name): name is string => Boolean(name))))];
    if (history.length) return `${history.join("、")}（逐日来源见图表悬浮）`;
  }
  const actual = [...new Set((panel.values || []).map(item => item.source).filter(Boolean))];
  if (actual.length) return actual.join("、");
  if (panel.id === "cn-limit-pool" || panel.id === "cn-limit-height") return "东方财富涨跌停池";
  if (panel.id === "cn-broken-board") return "东方财富炸板池";
  if (panel.id === "cn-limit-up-open-return") return "东方财富昨日涨停池＋腾讯指定交易日日线";
  if (panel.id.startsWith("cn-")) return "腾讯行情快照";
  return panel.id === "hk-market-cap" ? "" : "本机通达信港股日线";
}
function pointsFor(market: Market, field: NumericField): NamedSeries["points"] {
  return (overviews.value[market]?.points || []).map(point => {
    const value = point[field];
    const source = point.sources?.[field];
    let detail = source ? [source.label,
      typeof source.coverage === "number" ? `覆盖 ${source.coverage} 只` : "",
      typeof source.pairedCoverage === "number" ? `前收盘配对 ${source.pairedCoverage} 只` : "",
    ].filter(Boolean).join(" · ") : undefined;
    if (market === "hk" && point.coverageStatus === "INSUFFICIENT_LOCAL_FILES" &&
        ["turnoverYi", "advances", "unchanged", "declines"].includes(field)) {
      detail = `本机样本 ${point.turnoverCoverage ?? "—"} / 参考 ${point.referenceCoverage ?? "—"} 只，未达85%覆盖门`;
    }
    return { t: point.tradingDay, value: typeof value === "number" && Number.isFinite(value) ? value : null, detail };
  });
}
function single(market: Market, field: NumericField, name: string, unit: string, color: string, area = true): ChartSpec {
  return { series: [{ name, points: pointsFor(market, field) }], unit, type: "line", area, colors: [color] };
}
const charts = computed<Record<string, ChartSpec>>(() => {
  const palette = theme.palette;
  return {
    "cn-market-cap": single("cn", "totalMarketCapYi", "总市值", "亿元", palette.accent),
    "cn-float-market-cap": single("cn", "floatMarketCapYi", "流通市值", "亿元", palette.accent),
    "cn-turnover": single("cn", "turnoverYi", "成交额", "亿元", palette.info),
    "cn-breadth": { series: [
      { name: "上涨", points: pointsFor("cn", "advances") },
      { name: "下跌", points: pointsFor("cn", "declines") },
      { name: "平盘", points: pointsFor("cn", "unchanged") },
    ], unit: "家", type: "bar", area: false, colors: [palette.priceUp, palette.priceDown, palette.flat] },
    "cn-limit-pool": { series: [
      { name: "涨停", points: pointsFor("cn", "limitUpCount") },
      { name: "跌停", points: pointsFor("cn", "limitDownCount") },
    ], unit: "家", type: "bar", area: false, colors: [palette.priceUp, palette.priceDown] },
    "cn-broken-board": { series: [{ name: "炸板", points: pointsFor("cn", "failedLimitUpCount") }],
      unit: "家", type: "bar", area: false, colors: [palette.warning] },
    "cn-sealed-rate": single("cn", "sealedLimitUpRatePct", "封板率", "%", palette.info, false),
    "cn-limit-height": { series: [
      { name: "涨停最高连板", points: pointsFor("cn", "limitUpHeight") },
      { name: "跌停最高连板", points: pointsFor("cn", "limitDownHeight") },
    ], unit: "板", type: "line", area: false, colors: [palette.priceUp, palette.priceDown] },
    "cn-limit-up-open-return": single("cn", "yesterdayLimitUpOpenReturnPct", "开盘买入至收盘", "%", palette.warning, false),
    "hk-market-cap": single("hk", "totalMarketCapYi", "总市值", "亿港元", palette.accent),
    "hk-turnover": single("hk", "turnoverYi", "本机股票样本成交额", "亿港元", palette.info),
    "hk-breadth": { series: [
      { name: "上涨", points: pointsFor("hk", "advances") },
      { name: "下跌", points: pointsFor("hk", "declines") },
      { name: "平盘", points: pointsFor("hk", "unchanged") },
    ], unit: "家", type: "bar", area: false, colors: [palette.priceUp, palette.priceDown, palette.flat] },
  };
});
function latestComplete(market: Market, fields: NumericField[]): EquityPoint | undefined {
  return [...(overviews.value[market]?.points || [])].reverse().find(point =>
    fields.every(field => typeof point[field] === "number" && Number.isFinite(point[field]) && (point[field] as number) >= 0));
}
const compositions = computed<Record<string, CompositionItem[]>>(() => {
  const palette = theme.palette;
  const make = (market: Market, fields: Array<[NumericField, string, string]>): CompositionItem[] => {
    const point = latestComplete(market, fields.map(item => item[0]));
    return point ? fields.map(([field, name, color]) => ({ name, value: point[field] as number, color })) : [];
  };
  return {
    "cn-breadth": make("cn", [["advances", "上涨", palette.priceUp], ["declines", "下跌", palette.priceDown], ["unchanged", "平盘", palette.flat]]),
    "cn-limit-pool": make("cn", [["limitUpCount", "涨停", palette.priceUp], ["limitDownCount", "跌停", palette.priceDown]]),
    "hk-breadth": make("hk", [["advances", "上涨", palette.priceUp], ["declines", "下跌", palette.priceDown], ["unchanged", "平盘", palette.flat]]),
  };
});
const compositionDays = computed<Record<string, string | undefined>>(() => ({
  "cn-breadth": latestComplete("cn", ["advances", "declines", "unchanged"])?.tradingDay,
  "cn-limit-pool": latestComplete("cn", ["limitUpCount", "limitDownCount"])?.tradingDay,
  "hk-breadth": latestComplete("hk", ["advances", "declines", "unchanged"])?.tradingDay,
}));
function previewPoints(id: string): NamedSeries["points"] {
  return miniSeries.value[id]?.map(point => ({ t: point.day, value: point.value })) || [];
}
function compactPath(id: string): string {
  const points = miniSeries.value[id] || [];
  const values = points.flatMap(point => typeof point.value === "number" && Number.isFinite(point.value) ? [point.value] : []);
  if (!values.length) return "";
  const low = Math.min(...values), high = Math.max(...values);
  const padding = (high - low || Math.max(Math.abs(high), 1)) * .12;
  let continuing = false;
  const path = points.map((point, index) => {
    if (typeof point.value !== "number" || !Number.isFinite(point.value)) { continuing = false; return ""; }
    const x = points.length === 1 ? 60 : 2 + 116 * index / (points.length - 1);
    const y = 26 - 24 * (point.value - low + padding) / (high - low + 2 * padding);
    const segment = `${continuing ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    continuing = true;
    return segment;
  }).filter(Boolean).join(" ");
  return points.length === 1 ? `${path} L60.1,14.1` : path;
}

async function loadSections(): Promise<void> {
  const request = ++sectionsRequest;
  sectionsLoading.value = true;
  sectionsError.value = "";
  try {
    const payload = await apiGet<{ sections: SectionPayload[] }>("/api/data/sections", undefined,
      { ttlMs: 5 * 60_000, persist: true, force: true });
    if (request === sectionsRequest) sections.value = payload.sections;
  } catch (reason) {
    if (request === sectionsRequest) sectionsError.value = reason instanceof Error ? reason.message : "市场指标读取失败";
  } finally {
    if (request === sectionsRequest) sectionsLoading.value = false;
  }
}
async function loadOverview(market: Market): Promise<void> {
  const request = ++overviewRequest[market];
  overviewLoading.value[market] = true;
  overviewErrors.value[market] = "";
  try {
    const payload = await apiGet<EquityOverview>("/api/data/equities/" + market + "/overview", undefined,
      { ttlMs: 5 * 60_000, persist: true, force: true });
    if (request !== overviewRequest[market]) return;
    overviews.value = { ...overviews.value, [market]: payload };
    if (market === "hk" && payload.available) await loadSections();
  } catch (reason) {
    if (request === overviewRequest[market]) overviewErrors.value[market] = reason instanceof Error ? reason.message : "市场走势读取失败";
  } finally {
    if (request === overviewRequest[market]) overviewLoading.value[market] = false;
  }
}
async function loadExchangeOverview(): Promise<void> {
  const request = ++exchangeRequest;
  exchangeLoading.value = true;
  exchangeError.value = "";
  try {
    const payload = await apiGet<ExchangeOverview>("/api/data/exchanges/overview", undefined,
      { ttlMs: 5 * 60_000, persist: true, force: true });
    if (request === exchangeRequest) exchangeOverview.value = payload;
  } catch (reason) {
    if (request === exchangeRequest) exchangeError.value = reason instanceof Error ? reason.message : "交易所官网统计读取失败";
  } finally {
    if (request === exchangeRequest) exchangeLoading.value = false;
  }
}
async function loadPreviews(): Promise<void> {
  const request = ++previewsRequest;
  try {
    const payload = await apiGet<{ items: EconomyPreview[] }>("/api/data/tdx-economy/previews", { limit: 40 },
      { ttlMs: 5 * 60_000, persist: true, force: true });
    if (request === previewsRequest) miniSeries.value = Object.fromEntries(payload.items.map(item => [item.id, item.points]));
  } catch {
    // Catalog values remain visible while mini charts show explicit empty states.
  }
}
async function loadCatalog(): Promise<void> {
  const request = ++catalogRequest;
  economyCatalogLoading.value = true;
  economyError.value = "";
  try {
    const payload = await apiGet<{ items: EconomyItem[] }>("/api/data/tdx-economy/catalog", undefined,
      { ttlMs: 5 * 60_000, persist: true, force: true });
    if (request !== catalogRequest) return;
    economyCatalog.value = payload.items;
    miniSeries.value = {};
    const previousId = selectedEconomyId.value;
    if (!payload.items.some(item => item.id === previousId && item.available)) {
      selectedEconomyId.value = payload.items.find(item => item.id === "TDX_MACRO.3_PMI" && item.available)?.id
        || payload.items.find(item => item.available)?.id || "";
    }
    if (selectedEconomyId.value && selectedEconomyId.value === previousId) void loadSelectedSeries();
    void loadPreviews();
  } catch (reason) {
    if (request === catalogRequest) economyError.value = reason instanceof Error ? reason.message : "经济数据目录读取失败";
  } finally {
    if (request === catalogRequest) economyCatalogLoading.value = false;
  }
}
async function loadSelectedSeries(): Promise<void> {
  if (!selectedEconomyId.value) return;
  const request = ++seriesRequest;
  economySeriesLoading.value = true;
  economyError.value = "";
  try {
    const payload = await apiGet<EconomySeries>("/api/data/tdx-economy/series", { id: selectedEconomyId.value },
      { ttlMs: 5 * 60_000, persist: true, force: true });
    if (request === seriesRequest) economySeries.value = payload;
  } catch (reason) {
    if (request === seriesRequest) economyError.value = reason instanceof Error ? reason.message : "经济序列读取失败";
  } finally {
    if (request === seriesRequest) economySeriesLoading.value = false;
  }
}
async function loadAuxiliary(): Promise<void> {
  auxiliaryLoading.value = true;
  const outcomes = await Promise.allSettled([
    apiGet<THSCPI>("/api/data/ths-cpi", undefined, { force: true }),
    apiGet<SearchInterestSources>("/api/data/search-interest-sources", undefined, { force: true }),
    apiGet<RiskSignalCategories>("/api/data/risk-signal-categories", undefined, { force: true }),
    apiGet<THSIndexFlashStatus>("/api/data/ths-indexflash/status", undefined, { force: true }),
  ]);
  if (outcomes[0].status === "fulfilled") thsCPI.value = outcomes[0].value;
  if (outcomes[1].status === "fulfilled") searchInterest.value = outcomes[1].value;
  if (outcomes[2].status === "fulfilled") riskCategories.value = outcomes[2].value;
  if (outcomes[3].status === "fulfilled") thsFlash.value = outcomes[3].value;
  auxiliaryError.value = outcomes.some(item => item.status === "rejected") ? "部分观察来源暂不可用，请稍后重试。" : "";
  await loadDisclosure();
  auxiliaryLoading.value = false;
}
async function loadBlsPayroll(): Promise<void> {
  try {
    blsPayroll.value = await apiGet<BlsPayrollSeries>("/api/data/macro/series", { seriesId: "US_NONFARM_BLS_LATEST" }, { force: true });
    blsPayrollError.value = false;
  } catch { blsPayrollError.value = true; }
}
async function loadKplStatus(): Promise<void> {
  const request = ++kplRequest;
  const [status, history] = await Promise.allSettled([
    apiGet<KplMarketStatus>("/api/data/kpl-market/status", undefined, { force: true }),
    apiGet<KplHistory>("/api/data/kpl-market/history", undefined, { force: true }),
  ]);
  if (request !== kplRequest) return;
  if (status.status === "fulfilled") kplMarket.value = status.value;
  if (history.status === "fulfilled") kplHistory.value = history.value;
  kplReadError.value = status.status === "rejected" || history.status === "rejected";
}
async function loadThsEtf(): Promise<void> {
  const request = ++thsEtfRequest;
  try {
    const result = await apiGet<ThsEtfObservation>("/api/data/ths-app-etf/observation", undefined, { force: true });
    if (request === thsEtfRequest) { thsEtf.value = result; thsEtfError.value = false; }
  } catch {
    if (request === thsEtfRequest) thsEtfError.value = true;
  }
}
async function loadThsEtfStatus(): Promise<void> {
  const request = ++thsEtfStatusRequest;
  try {
    const result = await apiGet<ThsEtfUpdateStatus>("/api/data/ths-app-etf/status", undefined, { force: true });
    if (request === thsEtfStatusRequest) thsEtfUpdate.value = result;
  } catch {
    if (request === thsEtfStatusRequest) thsEtfUpdate.value = undefined;
  }
}
async function loadDisclosure(): Promise<void> {
  try {
    disclosureWatch.value = await apiGet<DisclosureWatch>("/api/data/disclosure-watch",
      disclosureKeyword.value ? { keyword: disclosureKeyword.value } : undefined, { force: true });
  } catch {
    auxiliaryError.value = "巨潮公告观察暂不可用；已保存的市场图表不受影响。";
  }
}
async function checkDataUpdateStatus(): Promise<void> {
  const request = ++updateStatusRequest;
  try {
    const current = await apiGet<DataUpdateStatus>("/api/data/update-status", undefined, { force: true });
    if (request !== updateStatusRequest) return;
    const previous = dataUpdateStatus.value?.completedRevision;
    dataUpdateStatus.value = current;
    dataUpdateStatusError.value = false;
    if (previous && previous !== current.completedRevision) {
      await Promise.allSettled([loadSections(), loadOverview("cn"), loadOverview("hk"),
        loadExchangeOverview(), loadCatalog(), loadAuxiliary(), loadBlsPayroll()]);
    }
  } catch {
    if (request === updateStatusRequest) dataUpdateStatusError.value = true;
  }
}
async function requestDataUpdate(): Promise<void> {
  if (requestingSourceUpdate.value) return;
  requestingSourceUpdate.value = true;
  try {
    await apiPost("/api/data/check-updates");
    await checkDataUpdateStatus();
  } catch (reason) {
    auxiliaryError.value = reason instanceof Error ? reason.message : "提交更新失败";
  } finally {
    requestingSourceUpdate.value = false;
  }
}
async function refreshAll(): Promise<void> {
  if (refreshBusy.value) return;
  refreshBusy.value = true;
  for (const path of ["/api/data/sections", "/api/data/equities/cn/overview", "/api/data/equities/hk/overview",
    "/api/data/exchanges/overview",
    "/api/data/tdx-economy/catalog", "/api/data/tdx-economy/previews", "/api/data/tdx-economy/series",
    "/api/data/ths-cpi", "/api/data/ths-indexflash/status", "/api/data/kpl-market/status", "/api/data/disclosure-watch", "/api/data/search-interest-sources",
    "/api/data/risk-signal-categories"]) invalidateQuery(path);
  try {
    await Promise.allSettled([loadSections(), loadOverview("cn"), loadOverview("hk"),
      loadExchangeOverview(), loadCatalog(), loadAuxiliary(), loadBlsPayroll(), loadKplStatus(), loadThsEtf(), loadThsEtfStatus()]);
  } finally {
    refreshBusy.value = false;
  }
}
function scrollToLegacySection(): void {
  const id = route.query.section === "hk" ? "data-hk" : route.query.section === "other" ? "data-economy" : "";
  if (id) document.getElementById(id)?.scrollIntoView({ block: "start" });
}
watch(selectedEconomyId, () => { economySeries.value = undefined; void loadSelectedSeries(); });
watch(disclosureKeyword, () => { void loadDisclosure(); });
function checkVisibleData(): void {
  if (document.visibilityState !== "visible") return;
  void checkDataUpdateStatus();
  void loadKplStatus();
  void loadThsEtf();
  void loadThsEtfStatus();
}
onMounted(() => {
  void Promise.allSettled([loadSections(), loadOverview("cn"), loadOverview("hk"), loadExchangeOverview(), loadCatalog(),
    loadAuxiliary(), loadBlsPayroll(), loadKplStatus(), loadThsEtf(), loadThsEtfStatus(), checkDataUpdateStatus()]);
  dataStatusTimer = window.setInterval(checkVisibleData, 60_000);
  document.addEventListener("visibilitychange", checkVisibleData);
  void nextTick(() => scrollToLegacySection());
});
onBeforeUnmount(() => {
  updateStatusRequest++;
  if (dataStatusTimer !== undefined) window.clearInterval(dataStatusTimer);
  document.removeEventListener("visibilitychange", checkVisibleData);
});
</script>

<template>
  <main class="data-workspace" data-test="focused-data-page">
    <header class="page-heading">
      <div>
        <span class="workspace-eyebrow">市场、经济与公告观察</span>
        <h1 class="page-title">数据</h1>
        <p class="page-note">各市场与经济指标连续展示；图表按自身来源日期和单位绘制，缺值不补造。</p>
      </div>
      <div class="heading-actions"><span v-if="refreshBusy" class="refresh-state" role="status">正在更新</span><el-button :disabled="requestingSourceUpdate" data-test="data-check-updates" @click="void requestDataUpdate()">{{ requestingSourceUpdate ? '已提交' : '检查新数据' }}</el-button><el-button :disabled="refreshBusy" data-test="data-refresh" @click="void refreshAll()">重载页面数据</el-button></div>
    </header>
    <p v-if="dataUpdateStatusError" class="inline-status" role="status" data-test="data-update-status-error">更新状态暂不可用，{{ dataUpdateStatus ? '下方保留上次读取结果。' : '请稍后重试。' }}</p>
    <p v-if="dataUpdateStatus" class="source-update-status" data-test="data-update-status">{{ dataUpdateStatusError ? '上次读取：' : '' }}{{ dataUpdateStatus.autoEnabled ? '服务每日检查公开来源；A股只在已核验交易日更新' : '自动更新已关闭' }} · <span v-for="job in dataUpdateStatus.jobs" :key="job.kind">{{ job.kind === 'EQUITY_DASHBOARD_UPDATE' ? '市场与宏观' : '巨潮公告' }}：{{ job.status === 'PASS' ? '已完成' : job.status === 'QUEUED' ? '排队' : job.status === 'RUNNING' ? '更新中' : job.status === 'NOT_RUN' ? '尚未运行' : '部分失败或待核验' }} </span></p>
    <nav class="section-index" aria-label="数据页分区"><a href="#data-cn">A股</a><a href="#data-exchange-stocks">三所股票</a><a href="#data-exchange-funds">沪深基金</a><a href="#data-exchange-bonds">债券与回购</a><a href="#data-hk">港股</a><a href="#data-economy">经济数据与商品比价</a><a href="#data-watch">搜索与公告观察</a></nav>
    <section class="source-overview" aria-label="数据来源与观测日期" data-test="data-source-overview">
      <div class="source-overview-heading"><div><span class="workspace-eyebrow">SOURCE MAP</span><h2>来源与观测日期</h2></div><p>按各来源自身的交易日或统计月显示；页面打开不会直接发起上游抓取。</p></div>
      <div class="source-grid">
        <a v-for="source in sourceTiles" :key="source.id" :href="source.href" class="source-card"
          :data-test="'data-source-' + source.id" :data-tone="source.tone">
          <span class="source-name">{{ source.name }}</span><span class="source-status">{{ source.status }}</span>
          <strong>{{ source.observation || '暂无观测' }}</strong><small>{{ source.detail }}</small>
        </a>
      </div>
    </section>

    <template v-for="entry in marketSections" :key="entry.market">
    <section :id="entry.id" class="market-section" :data-test="'data-section-' + entry.market">
      <div class="section-heading"><div><h2>{{ entry.label }}</h2><p>{{ entry.description }}</p></div><span v-if="overviewLoading[entry.market]" class="refresh-state">正在更新走势</span></div>
      <div v-if="sectionsError" class="inline-status" role="status">{{ sectionsError }} <el-button text @click="void loadSections()">重试</el-button></div>
      <ResearchLoadingSkeleton v-if="sectionsLoading && !sections.length" kind="cards" :rows="2" />
      <div v-else class="market-chart-grid" :class="'market-chart-grid-' + entry.market">
        <article v-for="panel in panelsByMarket[entry.market]" :key="panel.id" class="panel market-chart-card"
          :class="{ wide: Boolean(compositions[panel.id]?.length), headline: entry.market === 'cn' && ['cn-market-cap', 'cn-float-market-cap', 'cn-turnover'].includes(panel.id) }" :data-test="'data-panel-' + panel.id"
          :data-status="panel.status" :data-chart-type="charts[panel.id]?.type">
          <header class="chart-card-header">
            <div><h3>{{ panel.title }}</h3><p v-if="panel.values?.length" class="observation-date">截至 {{ formatDate(panel.values[0]?.tradingDate) }}</p></div>
            <el-tag :type="panel.status === 'PASS' ? 'success' : panel.status === 'PARTIAL' ? 'warning' : 'info'" size="small" effect="plain">{{ statusLabel(panel) }}</el-tag>
          </header>
          <div v-if="panel.values?.length" class="latest-values">
            <div v-for="metric in panel.values" :key="metric.metricKey">
              <span>{{ metricLabel(metric) }}</span>
              <strong>{{ formatValue(metric.value) }}<small>{{ metric.unit || charts[panel.id]?.unit || '' }}</small></strong>
              <time v-if="panel.values.length > 1">{{ formatDate(metric.tradingDate) }}</time>
              <small v-if="typeof metric.coverage === 'number'" class="coverage-note">已覆盖 {{ formatValue(metric.coverage) }} 只</small>
              <small v-else-if="metric.coverage" class="coverage-note">有效样本 {{ metric.coverage.pricedCount }}/{{ metric.coverage.poolCount }}</small>
            </div>
          </div>
          <p v-if="panel.stale" class="stale-note">{{ panel.id === 'hk-market-cap' ? '已保存港交所档案最新观测日为' : '最近交易日缺报；图中最近可信观测为' }} {{ formatDate(panel.values?.[0]?.tradingDate) }}。</p>
          <ResearchLoadingSkeleton v-if="overviewLoading[entry.market] && !overviews[entry.market]" kind="chart" />
          <template v-else>
            <div v-if="compositions[panel.id]?.length" class="composition-layout">
              <CompositionDonutChart :title="panel.title + ' · ' + formatDate(compositionDays[panel.id]) + ' 构成'" :items="compositions[panel.id]" :height="230" />
              <SeriesChart :series="charts[panel.id]?.series || []" :unit="charts[panel.id]?.unit || ''"
                :chart-type="charts[panel.id]?.type || 'line'" :area="false" :colors="charts[panel.id]?.colors || []"
                :smooth="panel.id !== 'cn-limit-height'" :show-symbols="panel.id === 'cn-limit-height'"
                :zero-baseline="charts[panel.id]?.type === 'bar' || panel.id === 'cn-limit-height'"
                :initial-window-points="entry.market === 'hk' ? 120 : 0" :height="235" />
            </div>
            <SeriesChart v-else :series="charts[panel.id]?.series || []" :unit="charts[panel.id]?.unit || ''"
              :chart-type="charts[panel.id]?.type || 'line'" :area="charts[panel.id]?.area || false"
              :smooth="panel.id !== 'cn-limit-height'" :show-symbols="panel.id === 'cn-limit-height'"
              :zero-baseline="charts[panel.id]?.type === 'bar' || panel.id === 'cn-limit-height' || panel.id === 'cn-limit-up-open-return'"
              :colors="charts[panel.id]?.colors || []" :initial-window-points="entry.market === 'hk' ? 120 : 0" :height="245" />
          </template>
          <p v-if="panel.id === 'hk-turnover' && hkCoverageGaps.length" class="coverage-gap-note" data-test="hk-turnover-gap-note">
            最近样本不足已断线：<span v-for="(gap, index) in hkCoverageGaps" :key="gap.tradingDay">{{ index ? '；' : '' }}{{ formatDate(gap.tradingDay) }} {{ gap.turnoverCoverage ?? '—' }}/{{ gap.referenceCoverage ?? '—' }} 只</span>。
          </p>
          <p v-if="!panel.values?.length" class="metric-empty">{{ panel.limitations?.[0] || '当前没有通过核验的数据。' }}</p>
          <p v-if="panelSource(panel)" class="metric-source">来源：{{ panelSource(panel) }}</p>
          <details v-if="panel.limitations?.length && panel.values?.length" class="metric-help"><summary>覆盖与口径</summary><p v-for="item in panel.limitations" :key="item">{{ item }}</p></details>
        </article>
      </div>
      <div v-if="overviewErrors[entry.market]" class="inline-status" role="status">{{ overviewErrors[entry.market] }} <el-button text @click="void loadOverview(entry.market)">重试</el-button></div>
      <article v-if="entry.market === 'cn'" class="panel" data-test="ths-indexflash-status" style="padding: 16px; margin-top: 16px">
        <h3>同花顺行情中心</h3>
        <p class="metric-source">独立核验上涨、下跌、涨停、跌停及“昨日涨停今日收益”；该收益与上方开盘买入至收盘的接盘收益口径不同。</p>
        <p class="metric-empty" role="status">{{ !thsFlash ? '来源状态读取中或暂不可用。' : thsFlash.status === 'BLOCKED' ? '同花顺接口暂时拒绝访问，后台会重试；尚无可核验数据。' : thsFlash.status === 'UNVERIFIED' ? '已取得响应，交易日期与收盘口径仍待核验，尚未计入图表。' : thsFlash.status === 'FAILED' ? '同花顺接口请求或响应异常，后台会重试。' : '尚未取得可核验的接口响应。' }}</p>
        <p v-if="thsFlash?.checkedAt" class="metric-source">最近检查：{{ new Date(thsFlash.checkedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) }}（北京时间）</p>
      </article>
      <article v-if="entry.market === 'cn'" class="panel etf-panel" data-test="ths-app-etf">
        <h3>同花顺 ETF 行情观察</h3>
        <p class="metric-source">APP“跟踪指数过滤”的固定样本，非全部 ETF，新上市基金不会自动加入；行情时间未核验，不代表实时数据或收盘价格。页面只读取本机记录。</p>
        <p class="metric-source" data-test="ths-etf-update-status">{{ !thsEtfUpdate ? '自动更新状态暂不可用。' : !thsEtfUpdate.autoEnabled ? '服务自动更新未开启。' : '已核验交易日盘中每 15 分钟尝试更新，盘后再检查一次。' }} {{ thsEtfUpdate?.status === 'RUNNING' ? '正在采集。' : thsEtfUpdate?.status === 'UNVERIFIED' ? '最近一次采集完成，行情时间仍待核。' : thsEtfUpdate?.status === 'AUTH_EXPIRED' || thsEtfUpdate?.status === 'PROVIDER_REJECTED' || thsEtfUpdate?.status === 'SESSION_INVALID' ? '会话不可用或来源拒绝，自动请求已停止。' : thsEtfUpdate?.status === 'RATE_LIMITED' ? '来源限流，已延后重试。' : thsEtfUpdate?.status === 'FAILED' || thsEtfUpdate?.status === 'HTTP_ERROR' ? '最近一次采集失败，保留旧观察并稍后重试。' : '' }} <template v-if="thsEtfUpdate?.attemptAt">最近尝试：{{ etfTime(thsEtfUpdate.attemptAt) }}（北京时间）。</template></p>
        <p v-if="thsEtfError" class="inline-status" role="status" data-test="ths-etf-read-error">本机观察读取失败；{{ thsEtf?.available ? '下方保留上次读取结果，请以采集时间为准。' : '请稍后重试。' }}</p>
        <template v-if="thsEtf?.available">
          <p class="metric-source">采集时间 {{ etfTime(thsEtf.captureStart!) }} 至 {{ etfTime(thsEtf.captureEnd!) }}（北京时间）；不同分页并非同时采集，数据待核验。</p>
          <div class="etf-controls">
            <label class="etf-search">搜索 ETF <input v-model="thsEtfSearch" type="search" placeholder="代码或名称" /></label>
            <span class="etf-count" aria-live="polite" data-test="ths-etf-count">显示 {{ thsEtfRows.length }} / {{ thsEtf.count }} 只</span>
            <button v-if="etfSortField" class="etf-reset" type="button" @click="etfSortField = null">恢复采集顺序</button>
          </div>
          <div class="etf-table-wrap" role="region" aria-label="同花顺 ETF 观察表" tabindex="0">
            <table class="etf-table">
              <thead><tr><th scope="col">代码</th><th scope="col">名称</th>
                <th scope="col" :aria-sort="etfSortState('price')"><button type="button" @click="sortEtf('price')">观察价 <span aria-hidden="true">{{ etfSortField === 'price' ? (etfSortDirection === 'ascending' ? '↑' : '↓') : '↕' }}</span></button></th>
                <th scope="col" :aria-sort="etfSortState('changePct')"><button type="button" @click="sortEtf('changePct')">涨跌幅 <span aria-hidden="true">{{ etfSortField === 'changePct' ? (etfSortDirection === 'ascending' ? '↑' : '↓') : '↕' }}</span></button></th>
                <th scope="col" :aria-sort="etfSortState('turnover')"><button type="button" @click="sortEtf('turnover')">成交额（元） <span aria-hidden="true">{{ etfSortField === 'turnover' ? (etfSortDirection === 'ascending' ? '↑' : '↓') : '↕' }}</span></button></th>
                <th scope="col">采集时间（北京时间）</th></tr></thead>
              <tbody><tr v-for="row in thsEtfRows" :key="row.code"><td>{{ row.code.split(':')[1] }}</td><td>{{ row.name }}</td><td>{{ row.price ?? '—' }}</td><td :class="{ 'etf-up': row.changePct != null && row.changePct > 0, 'etf-down': row.changePct != null && row.changePct < 0 }">{{ row.changePct == null ? '—' : `${row.changePct.toFixed(2)}%` }}</td><td>{{ row.turnover == null ? '—' : row.turnover.toLocaleString('zh-CN') }}</td><td><time :datetime="row.observedAt">{{ etfTime(row.observedAt) }}</time></td></tr></tbody>
            </table>
            <p v-if="!thsEtfRows.length" class="metric-empty">没有符合条件的 ETF。</p>
          </div>
        </template>
        <p v-else-if="!thsEtfError" class="metric-empty">尚无可读取的 APP 行情观察。</p>
      </article>
      <article v-if="entry.market === 'cn'" class="panel" data-test="kpl-market-status" style="padding: 16px; margin-top: 16px">
        <h3>开盘啦市场情绪</h3>
        <p class="metric-source">独立来源，统计范围和字段口径待核验，不计入上方图表。</p>
        <p v-if="kplReadError" class="inline-status" role="status" data-test="kpl-read-error">本机来源状态或历史读取失败；已读取的旧结果会保留，请以来源日期和采集时间为准。</p>
        <p class="metric-empty" role="status">{{ !kplMarket ? '来源状态暂不可用。' : kplMarket.status === 'UNVERIFIED' ? '已取得样本，数据时间与统计口径仍待核验。' : kplMarket.status === 'BLOCKED' ? '来源暂时受阻，尚无可核验数据。' : kplMarket.status === 'FAILED' ? '来源检查失败，尚无可核验数据。' : kplMarket.status === 'EMPTY' ? '来源返回空数据，未计为零。' : kplMarket.status === 'STALE' ? '来源返回的行情日期不是预期交易日。' : kplMarket.status === 'NEVER' ? '尚未取得可核验的样本。' : '来源状态待核验。' }}</p>
        <p v-if="kplMarket?.observedDate" class="metric-source">来源返回日期：{{ kplMarket.observedDate }}（不代表行情更新时间）</p>
        <p v-if="kplMarket?.calendarStatus === 'UNKNOWN'" class="metric-source">当前年度交易日历尚未核验，自动采样会暂停。</p>
        <p v-if="kplMarket?.checkAge === 'OLD'" class="metric-source">最近检查已过期，请检查服务和自动更新状态；盘中超过 15 分钟或非盘中超过 7 天会提示。</p>
        <p v-if="kplMarket?.checkedAt" class="metric-source">最近检查：{{ new Date(kplMarket.checkedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) }}（北京时间）</p>
        <div class="kpl-history" data-test="kpl-market-history">
          <h4>开盘啦涨跌分析历史</h4>
          <p class="metric-source">开盘啦独立口径；不与上方东方财富序列混合。采集时刻不代表行情更新时间，数据不能视为已核验最终收盘。</p>
          <template v-if="kplHistory?.available">
            <p class="metric-source">来源最近日期：{{ kplHistory.latestDay }} · {{ kplHistory.latestPhase === 'INTRADAY' ? '当日盘中样本' : kplHistory.latestPhase === 'AFTER_CLOSE' ? '当日盘后观察' : '历史快照' }} · 此日期采集：{{ new Date(kplHistory.latestObservedAt || kplHistory.fetchedAt!).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) }}（北京时间）；历史日期来自多次独立采集。</p>
            <p v-if="kplHistory.calendarIncomplete" class="metric-source">部分年份交易日历未核验，未能识别所有缺报日。</p>
            <SeriesChart :series="kplCounts" unit="家" :area="false" :smooth="false" :height="235" />
            <SeriesChart :series="kplRate" unit="%" :area="false" :smooth="false" :height="190" />
          </template>
          <p v-else class="metric-empty">尚无可读取的开盘啦历史样本。</p>
        </div>
      </article>
      <details v-if="overviews[entry.market]?.limitations.length" class="source-details"><summary>{{ entry.label }}走势统计范围</summary><p v-for="item in overviews[entry.market]?.limitations" :key="item">{{ item }}</p></details>
    </section>
    <ExchangeMarketStats v-if="entry.market === 'cn'" :overview="exchangeOverview" :loading="exchangeLoading"
      :error="exchangeError" @retry="void loadExchangeOverview()" />
    </template>

    <section id="data-economy" class="economy-section" data-test="data-section-economy">
      <article class="panel" data-test="bls-payroll-series">
        <h2>美国非农就业月度变化（BLS 最新修订）</h2>
        <p>季调后就业人数相邻统计月之差，单位千人；这是当前修订值，不代表首次公布值或公布日期。</p>
        <div v-if="latestBlsPayroll" class="preview-value">{{ formatValue(latestBlsPayroll.value) }}<small>千人 · 统计月 {{ latestBlsPayroll.observationPeriod }}</small></div>
        <SeriesChart v-if="blsPayroll?.available" :series="blsPayrollTimeline" unit="千人" :initial-window-points="30" :height="260" />
        <p v-else class="metric-empty">{{ blsPayrollError ? '本机序列暂不可读取，请稍后重试。' : '尚无已入库的 BLS 观测。' }}</p>
        <p class="metric-source">来源：美国劳工统计局 BLS · CES0000000001；与按公布日期保存的既有非农序列分开。</p>
      </article>
      <div class="section-heading"><div><h2>通达信经济数据</h2><p>读取本机通过质量检查的金融终端 38# 序列，保留原始观测值与日期。</p></div><span class="muted">{{ economyCatalog.filter(item => item.group === 'economy').length }} 条已接入</span></div>
      <ResearchLoadingSkeleton v-if="economyCatalogLoading && !economyCatalog.length" kind="cards" :rows="2" />
      <div v-else class="preview-grid">
        <article v-for="item in featuredItems" :key="item.id" class="panel preview-card" :data-test="'data-economy-' + item.id">
          <div class="preview-heading"><h3>{{ item.name }}</h3><span>{{ formatDate(item.latestDay) }}</span></div>
          <div class="preview-value">{{ formatValue(item.latestValue) }}<small>{{ item.unit || '终端原值' }}</small></div>
          <SparklineChart :points="previewPoints(item.id)" :height="118" />
          <p class="metric-source">通达信金融终端 · {{ item.topic }}</p>
        </article>
      </div>

      <div class="section-heading ratio-heading"><div><h2>商品比价</h2><p>同一来源、同一日期的外盘期货收盘价配对；三条比价走势同时展示。</p></div></div>
      <div class="ratio-chart-grid">
        <article v-for="item in ratioItems" :key="item.id" class="panel preview-card" :data-test="'data-ratio-' + item.id">
          <div class="preview-heading"><h3>{{ item.name }}</h3><span>{{ formatDate(item.latestDay) }}</span></div>
          <div class="preview-value">{{ formatValue(item.latestValue) }}<small>{{ item.unit || '待接入' }}</small></div>
          <SparklineChart :points="previewPoints(item.id)" :height="135" />
          <p class="metric-source">{{ item.source || '等待同日配对来源' }}</p>
        </article>
      </div>

      <div class="economy-browser panel">
        <div class="economy-browser-heading"><div><h2>经济指标目录</h2><p>全部已接入通达信经济指标列在下方；点选名称查看完整历史图。</p></div><el-input v-model="economySearch" clearable placeholder="搜索指标名称或代码" aria-label="搜索经济指标" class="economy-search" /></div>
        <div v-for="group in economyGroups" :key="group.topic" class="economy-group">
          <h3>{{ group.topic }} <span>{{ group.items.length }}</span></h3>
          <div class="economy-directory">
            <button v-for="item in group.items" :key="item.id" type="button" :class="{ selected: selectedEconomyId === item.id }" :data-test="'economy-item-' + item.id" @click="selectedEconomyId = item.id">
              <span>{{ item.name }}</span><strong>{{ formatValue(item.latestValue) }}</strong><small>{{ item.unit || '终端原值' }} · {{ formatDate(item.latestDay) }}</small>
              <svg v-if="compactPath(item.id)" class="compact-trend" viewBox="0 0 120 28" role="img" :aria-label="item.name + '最近观测走势'"><path :d="compactPath(item.id)" /></svg>
              <small v-else class="directory-no-trend">暂无历史走势</small>
            </button>
          </div>
        </div>
        <p v-if="economyError" class="inline-status" role="status">{{ economyError }} <el-button text @click="void (economyCatalog.length ? loadSelectedSeries() : loadCatalog())">重试</el-button></p>
      </div>
      <div class="panel selected-economy-chart" data-test="data-selected-economy-chart">
        <div class="economy-browser-heading"><div><h2>{{ selectedEconomy?.name || '选择经济指标' }}历史走势</h2><p v-if="selectedEconomy">来源：{{ selectedEconomy.source }} · 最近观测 {{ formatDate(selectedEconomy.latestDay) }} · {{ selectedEconomy.unit || '终端原值' }}</p></div></div>
        <ResearchLoadingSkeleton v-if="economySeriesLoading && !economySeries" kind="chart" />
        <SeriesChart v-else :series="selectedTimeline" :unit="economySeries?.unit || ''" :initial-window-points="120" :height="330" />
        <p v-if="economySeries?.limitations?.length" class="metric-source">{{ economySeries.limitations.join(" ") }}</p>
      </div>
      <div class="panel selected-economy-chart" data-test="data-ths-cpi">
        <div class="economy-browser-heading"><div><h2>同花顺 CPI（月度独立来源）</h2><p>与通达信金融终端 38# CPI 分开记录；来源表按月公布，未公布月份不补值。</p></div></div>
        <div v-if="latestThsCPI" class="latest-values"><div><span>全国当月指数</span><strong>{{ formatValue(latestThsCPI.index) }}</strong></div><div><span>同比</span><strong>{{ formatValue(latestThsCPI.yoyPct) }}<small>%</small></strong></div><div><span>环比</span><strong>{{ formatValue(latestThsCPI.momPct) }}<small>%</small></strong></div><small>观测月 {{ latestThsCPI.month }}</small></div>
        <SeriesChart :series="cpiTimeline" unit="指数" :initial-window-points="30" :height="260" />
        <p v-if="!thsCPI?.available" class="metric-empty">同花顺月度 CPI 尚无已保存观测。</p>
        <p class="metric-source">来源：<a href="https://data.10jqka.com.cn/macro/cpi/" target="_blank" rel="noopener noreferrer">同花顺公开 CPI 表</a> · {{ thsCPI?.limitations?.join(' ') || '月度口径待导入' }}</p>
      </div>
    </section>

    <section id="data-watch" class="economy-section" data-test="data-section-watch">
      <div class="section-heading"><div><h2>搜索与公告观察</h2><p>公开来源的可复现数值与公告线索分开记录；关键词命中不代表个股已被认定有风险。</p></div></div>
      <div class="panel watch-panel" data-test="data-search-interest">
        <h3>投资搜索热度</h3><p>关注词：{{ searchInterest?.terms.join('、') || '炒股、股票、基金、期货、开户' }}</p>
        <div class="watch-source-grid"><article v-for="source in searchInterest?.sources || []" :key="source.id"><strong>{{ source.name }}</strong><small>当前无可核验逐日序列</small><p>{{ source.reason }}</p><a :href="source.url" target="_blank" rel="noopener noreferrer">查看来源说明</a></article></div>
      </div>
      <div class="panel watch-panel" data-test="data-disclosure-watch">
        <div class="economy-browser-heading"><div><h3>巨潮公告关键词观察</h3><p>公开原公告标题和日期；结果均为待核查线索，不作“停牌”“退市”或新规风险的自动认定。</p></div><el-select v-model="disclosureKeyword" clearable aria-label="筛选公告关键词" placeholder="全部关键词"><el-option v-for="term in [...(disclosureWatch?.keywords || []), ...(disclosureWatch?.riskSignalTerms || [])]" :key="term" :label="term" :value="term" /></el-select></div>
        <p class="metric-source">已记录 {{ disclosureWatch?.total || 0 }} 条匹配 · 最近抓取 {{ disclosureWatch?.lastFetchedAt ? formatDate(disclosureWatch.lastFetchedAt) : '暂无' }}</p>
        <div v-if="disclosureWatch?.items.length" class="disclosure-list"><article v-for="item in disclosureWatch.items" :key="item.announcementId + '-' + item.keyword"><time>{{ item.publicationDay }}</time><strong>{{ item.securityCode }} {{ item.securityName }}</strong><span>{{ item.keyword }} · 待核查</span><a :href="item.pdfUrl" target="_blank" rel="noopener noreferrer">{{ item.title }}</a></article></div>
        <p v-else class="metric-empty">{{ disclosureWatch?.available ? '当前筛选条件没有已保存的关键词命中，不等于没有风险公告。' : '尚未保存巨潮公告检索结果。' }}</p>
      </div>
      <div class="panel watch-panel" data-test="data-risk-signal-categories">
        <h3>新规风险线索核查项</h3><p>以下只说明还需补齐的证据；不同上市板块适用规则不同，公告关键词命中不能产生个股风险名单。</p>
        <div class="watch-source-grid"><article v-for="item in riskCategories?.categories || []" :key="item.id"><strong>{{ item.name }}</strong><small>检索词：{{ item.terms.join('、') }}</small><p>{{ item.requiredEvidence }}</p></article></div>
        <p class="metric-source">适用规则需逐板核查：<a v-for="source in riskCategories?.ruleSources || []" :key="source.board" :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.board }} </a><span>北交所规则及过渡安排仍待逐条核验。</span></p>
      </div>
      <p v-if="auxiliaryError" class="inline-status" role="status">{{ auxiliaryError }}</p>
    </section>
  </main>
</template>

<style scoped>
.data-workspace { min-width: 0; max-width: 1500px; margin: 0 auto; }
.workspace-eyebrow { display: block; margin-bottom: 5px; color: var(--ml-text-disabled); font-size: 11px; letter-spacing: .06em; }
.page-note, .section-heading p, .economy-browser-heading p { color: var(--ml-text-secondary); font-size: 13px; line-height: 1.6; }
.page-note { margin: 8px 0 0; }
.source-update-status { color: var(--ml-text-secondary); font-size: 12px; line-height: 1.6; }
.heading-actions { display: flex; align-items: center; gap: 10px; }
.refresh-state { color: var(--ml-accent); font-size: 12px; }
.section-index { display: flex; gap: 9px; flex-wrap: wrap; margin: 18px 0; }
.section-index a { padding: 8px 14px; border: 1px solid var(--ml-divider); border-radius: 8px; background: var(--ml-surface); color: var(--ml-text-secondary); text-decoration: none; font-size: 12px; }
.section-index a:hover, .section-index a:focus-visible { border-color: var(--ml-accent); color: var(--ml-accent); }
.source-overview { margin: 10px 0 30px; padding: 18px; border: 1px solid var(--ml-divider); border-radius: 12px; background: var(--ml-surface); }
.source-overview-heading { display: flex; justify-content: space-between; align-items: end; gap: 18px; margin-bottom: 15px; }
.source-overview-heading h2 { margin: 2px 0 0; font-size: 17px; }
.source-overview-heading p { max-width: 530px; margin: 0; color: var(--ml-text-secondary); font-size: 12px; line-height: 1.5; }
.source-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.source-card { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 8px; min-width: 0; padding: 13px 14px; border: 1px solid var(--ml-divider); border-radius: 9px; background: var(--ml-surface-elevated); color: var(--ml-text-primary); text-decoration: none; }
.source-card:hover, .source-card:focus-visible { border-color: var(--ml-accent); }
.source-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 600; }
.source-status { color: var(--ml-text-secondary); font-size: 10px; white-space: nowrap; }
.source-card[data-tone="ready"] .source-status { color: var(--ml-price-down); }
.source-card[data-tone="partial"] .source-status { color: var(--ml-warning); }
.source-card strong { grid-column: 1 / -1; font-size: 18px; line-height: 1.2; font-variant-numeric: tabular-nums; }
.source-card small { grid-column: 1 / -1; color: var(--ml-text-secondary); font-size: 10px; line-height: 1.4; }
.market-section, .economy-section { scroll-margin-top: 72px; margin-top: 28px; }
.section-heading { display: flex; justify-content: space-between; align-items: end; gap: 12px; margin: 0 0 15px; }
.section-heading h2 { margin: 0; font-size: 21px; }
.section-heading p { margin: 5px 0 0; }
.muted { color: var(--ml-text-secondary); font-size: 12px; }
.market-chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 15px; }
.market-chart-card { min-width: 0; padding: 16px 18px; background: var(--ml-surface); }
.market-chart-card.wide { grid-column: 1 / -1; }
.market-chart-grid-cn { grid-template-columns: repeat(6, minmax(0, 1fr)); }
.market-chart-grid-cn .market-chart-card { grid-column: span 3; }
.market-chart-grid-cn .market-chart-card.headline { grid-column: span 2; }
.market-chart-grid-cn .market-chart-card.wide { grid-column: 1 / -1; }
.market-chart-grid-hk { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.chart-card-header, .preview-heading { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
.chart-card-header h3, .preview-heading h3 { margin: 0; font-size: 15px; }
.observation-date, .preview-heading span { margin: 5px 0 0; color: var(--ml-text-disabled); font-size: 11px; }
.latest-values { display: flex; flex-wrap: wrap; gap: 14px 26px; margin: 16px 0 6px; }
.latest-values > div { display: grid; align-content: start; gap: 4px; }
.latest-values span, .latest-values time, .coverage-note { color: var(--ml-text-secondary); font-size: 11px; }
.latest-values strong { font-size: 23px; line-height: 1.1; font-weight: 650; font-variant-numeric: tabular-nums; }
.latest-values strong small, .preview-value small { margin-left: 5px; color: var(--ml-text-secondary); font-size: 11px; font-weight: 400; }
.composition-layout { display: grid; grid-template-columns: minmax(210px, .36fr) minmax(0, 1fr); gap: 12px; align-items: center; }
.metric-empty, .inline-status { color: var(--ml-text-secondary); font-size: 12px; line-height: 1.6; }
.coverage-gap-note { margin: 10px 0 0; padding: 8px 10px; border-left: 2px solid var(--ml-warning); background: var(--ml-surface-elevated); color: var(--ml-text-secondary); font-size: 11px; line-height: 1.5; }
.inline-status, .stale-note { color: var(--ml-warning); }
.stale-note { margin: 10px 0 0; font-size: 12px; }
.metric-source { margin: 8px 0 0; color: var(--ml-text-disabled); font-size: 11px; line-height: 1.5; }
.etf-panel { margin-top: 16px; padding: 18px; }
.etf-panel h3 { margin: 0; font-size: 15px; }
.etf-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 16px; margin-top: 15px; }
.etf-search { display: flex; align-items: center; gap: 9px; color: var(--ml-text-secondary); font-size: 12px; }
.etf-search input { width: min(235px, 55vw); padding: 7px 10px; border: 1px solid var(--ml-divider); border-radius: 6px; background: var(--ml-surface-elevated); color: var(--ml-text-primary); }
.etf-search input:focus-visible, .etf-table-wrap:focus-visible, .etf-table button:focus-visible, .etf-reset:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
.etf-count { color: var(--ml-text-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
.etf-reset, .etf-table button { border: 0; padding: 0; background: transparent; color: var(--ml-accent); font: inherit; cursor: pointer; }
.etf-reset { font-size: 12px; }
.etf-table-wrap { max-height: 360px; overflow: auto; margin-top: 12px; border: 1px solid var(--ml-divider); border-radius: 6px; }
.etf-table { width: 100%; min-width: 740px; border-collapse: collapse; text-align: left; font-size: 12px; font-variant-numeric: tabular-nums; }
.etf-table th { position: sticky; top: 0; z-index: 1; background: var(--ml-surface-elevated); color: var(--ml-text-secondary); font-weight: 500; }
.etf-table th, .etf-table td { padding: 9px 12px; border-bottom: 1px solid var(--ml-divider); white-space: nowrap; }
.etf-table tr:last-child td { border-bottom: 0; }
.etf-table th:nth-child(n+3):nth-child(-n+5), .etf-table td:nth-child(n+3):nth-child(-n+5) { text-align: right; }
.etf-table button { color: inherit; white-space: nowrap; }
.etf-table button:hover { color: var(--ml-accent); }
.etf-table th[aria-sort="ascending"] button, .etf-table th[aria-sort="descending"] button { color: var(--ml-accent); }
.etf-up { color: var(--ml-price-up); }
.etf-down { color: var(--ml-price-down); }
.etf-table-wrap > .metric-empty { padding: 0 12px; }
.metric-help, .source-details { margin-top: 10px; color: var(--ml-text-secondary); font-size: 12px; line-height: 1.6; }
.metric-help p, .source-details p { margin: 7px 0 0; }
.preview-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.preview-card { min-width: 0; padding: 15px; background: var(--ml-surface); }
.preview-value { margin-top: 13px; font-size: 26px; line-height: 1.15; font-weight: 650; font-variant-numeric: tabular-nums; }
.ratio-heading { margin-top: 28px; }
.ratio-chart-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.watch-panel { margin-top: 15px; padding: 18px; background: var(--ml-surface); }
.watch-panel h3 { margin: 0 0 6px; font-size: 16px; }
.watch-panel > p, .watch-panel .economy-browser-heading p { color: var(--ml-text-secondary); font-size: 12px; }
.watch-source-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
.watch-source-grid article { display: grid; gap: 5px; border: 1px solid var(--ml-divider); border-radius: 8px; padding: 12px; }
.watch-source-grid small, .disclosure-list time, .disclosure-list span { color: var(--ml-text-secondary); font-size: 11px; }
.watch-source-grid a, .disclosure-list a { color: var(--ml-accent); font-size: 12px; overflow-wrap: anywhere; }
.disclosure-list { display: grid; gap: 8px; max-height: 490px; overflow: auto; margin-top: 13px; }
.disclosure-list article { display: grid; grid-template-columns: 90px 150px 135px minmax(0, 1fr); gap: 8px; align-items: baseline; border-bottom: 1px solid var(--ml-divider); padding: 9px 0; font-size: 12px; }
.economy-browser, .selected-economy-chart { margin-top: 22px; padding: 18px; background: var(--ml-surface); }
.economy-browser-heading { display: flex; justify-content: space-between; align-items: end; gap: 18px; }
.economy-browser-heading h2 { margin: 0; font-size: 17px; }
.economy-browser-heading p { margin: 5px 0 0; }
.economy-search { width: min(330px, 100%); }
.economy-group { margin-top: 22px; }
.economy-group h3 { margin: 0 0 10px; font-size: 14px; }
.economy-group h3 span { color: var(--ml-text-disabled); font-weight: 400; }
.economy-directory { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.economy-directory button { display: grid; justify-items: start; gap: 5px; min-width: 0; padding: 11px 12px; border: 1px solid var(--ml-divider); border-radius: 8px; background: var(--ml-surface-elevated); color: var(--ml-text-primary); text-align: left; cursor: pointer; }
.economy-directory button:hover, .economy-directory button.selected { border-color: var(--ml-accent); background: var(--ml-surface-selected); }
.economy-directory button span { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.economy-directory button strong { font-size: 15px; font-variant-numeric: tabular-nums; }
.economy-directory button small { color: var(--ml-text-disabled); font-size: 10px; }
.compact-trend { width: 100%; height: 30px; overflow: visible; }
.compact-trend path { fill: none; stroke: var(--ml-accent); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.directory-no-trend { align-self: end; }
@media (max-width: 1050px) { .source-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .market-chart-grid-cn, .market-chart-grid-hk, .preview-grid, .ratio-chart-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .market-chart-grid-cn .market-chart-card, .market-chart-grid-cn .market-chart-card.headline { grid-column: auto; } .market-chart-grid-cn .market-chart-card.wide { grid-column: 1 / -1; } .economy-directory { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 720px) { .source-overview-heading { display: block; } .source-overview-heading p { margin-top: 6px; } .source-grid, .market-chart-grid, .preview-grid, .ratio-chart-grid { grid-template-columns: 1fr; } .market-chart-card.wide, .market-chart-grid-cn .market-chart-card.wide { grid-column: auto; } .composition-layout { grid-template-columns: 1fr; } .economy-directory { grid-template-columns: repeat(2, minmax(0, 1fr)); } .economy-browser-heading { display: grid; } .economy-search { width: 100%; } }
@media (max-width: 720px) { .watch-source-grid, .disclosure-list article { grid-template-columns: 1fr; } }
</style>
