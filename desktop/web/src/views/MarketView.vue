<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  shallowRef,
  watch,
} from "vue";
import { storeToRefs } from "pinia";
import { useRoute, useRouter } from "vue-router";
import KLineChart, {
  type ChartDrawing,
  type ChartDrawingStyle,
  type DrawingLineStyle,
  type DrawingTool,
  type FibonacciRetracementLevel,
  type KLineBar,
  type StrategyChartMarker,
} from "../components/charts/KLineChart.vue";
import DrawingColorPicker from "../components/charts/DrawingColorPicker.vue";
import MarketLoadingSkeleton from "../components/charts/MarketLoadingSkeleton.vue";
import { DRAWING_COLOR_PRESETS } from "../components/charts/drawingPalette";
import ChartIcon from "../components/charts/ChartIcon.vue";
import ChartMenu from "../components/charts/ChartMenu.vue";
import { chartTypes, chartTypeGroups, type ChartType } from "../domain/chartPresentation";
import { mergeBarTail, prependHistoryPage } from "../domain/chartLayout";
import { useChartReplay } from "../composables/useChartReplay";
import { replayRange, REPLAY_SPEEDS, type ChartRange } from "../domain/chartReplay";
import {
  DEFAULT_MARKET_CATEGORY,
  editableTarget,
  nextSortState,
  type SortableMarketInstrument,
} from "../domain/marketList";
import {
  apiGet,
  ApiRequestError,
  apiPost,
  apiPut,
  formatNumber,
  invalidateQuery,
  peekCachedQuery,
} from "../domain/api";
import type {
  IndicatorInstance,
  IndicatorParameterDefinition,
  VolumeProfile,
} from "../domain/strategyTypes";
import { useMarketStore } from "../stores/market";
import { formatMarketField, formatQuote, isFiniteQuote, marketFieldTone } from "../domain/marketQuote";

interface Instrument extends SortableMarketInstrument {
  instrumentId: string;
  symbol?: string;
  name?: string;
  market?: string;
  assetType?: string;
  seriesKind?: string;
  period?: string;
  latestPrice?: number;
  lastClose?: number;
  totalMarketCap?: number;
  floatMarketCap?: number;
  notionalValue?: number;
  officialMetricsDate?: string;
  specMarginRateLong?: number;
  specMarginRateShort?: number;
  marginRateUsed?: number;
  marginPublishedOn?: string | string[];
  marginCarriedForward?: boolean;
  openInterest?: number;
  capitalDeposit?: number;
  capitalDepositLong?: number;
  capitalDepositShort?: number;
  capitalDepositReason?: string;
  comparisonStatus?: string;
  comparisonIssues?: Record<string, { tdx: number; exchange: number }>;
  sourcePolicyVersion?: string;
  marginSourceKind?: string;
  officialMetricsStatus?: string;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  settlement?: number;
  volume?: number;
  amount?: number;
  pctChange?: number;
  amplitude?: number;
  dailyReturns?: Record<string, number | null>;
  fieldCapabilities?: Record<string, boolean>;
  matchedStrategyIds?: string[];
  nightSession?: string;
  actualSource?: string;
  source?: string;
}
interface MarketCategory {
  id: string;
  label: string;
}
interface BarsMeta {
  total: number;
  period: string;
  availablePeriods: string[];
  earliestBarAt?: string;
  latestBarAt?: string;
  dataVersion?: string;
}
interface History extends BarsMeta {
  start: number;
  size: number;
  bars: KLineBar[];
  before?: string;
  hasMore?: boolean;
}
interface ChartBootstrap extends History {
  series: Record<string, Array<number | null>>;
  drawings: ChartDrawing[];
}
interface IndicatorCatalogItem {
  resourceKind?: "indicator" | "drawing_tool";
  id: string;
  version: number;
  name: string;
  englishName?: string;
  category?: string;
  categoryLabel?: string;
  placement: "overlay" | "pane";
  status: string;
  unavailableReason?: string;
  supportedAssetTypes?: string[];
  parameters: IndicatorParameterDefinition[];
}
interface IndicatorSeriesResponse {
  instances: IndicatorInstance[];
}
interface ActiveChartStrategy {
  strategyId: string;
  strategyVersion: number;
  displayName: string;
  parameters: Record<string, string | number | boolean>;
}
interface StrategyTrade {
  tradeId: string;
  instrumentId: string;
  direction: "long" | "short";
  entryTime: string;
  exitTime: string;
  entryPrice: number;
  exitPrice: number;
  quantity: number;
  commission: number;
  slippageCost: number;
  pnl: number;
  pnlPercent: number | null;
  holdingBars: number;
  exitReason: string;
  currency: string;
}
interface StrategyReport {
  reportId: string;
  runId: string;
  currency: string;
  period: { barsCount: number };
  metrics: Record<string, number | null>;
  metricUnavailableReasons: Record<string, string>;
  markers: StrategyChartMarker[];
  trades: StrategyTrade[];
}
interface StrategyBacktestResponse {
  runId: string;
  status: string;
  barsCount: number;
  definitionHash: string;
  dependencyLock: {
    strategy: { id: string; version: number; definitionHash: string };
    strategyFunctions: Array<{ id: string; version: number; definitionHash: string }>;
    indicators: Array<{ id: string; version: number; definitionHash: string }>;
    marketData: { dataVersion: string; dataFingerprint: string };
  };
  report: StrategyReport;
}
interface Board {
  period: string;
  loadedPeriod: string;
  loadedVersion: string;
  bars: KLineBar[];
  loading: boolean;
  error: string;
  loadingEarlier: boolean;
  start: number;
  total: number;
  before?: string;
  hasMore: boolean;
  availablePeriods: string[];
  requestId: number;
  instrumentId: string;
  abort?: AbortController;
}
type ListColumnKey =
  | "symbol" | "name" | "latestPrice" | "totalMarketCap" | "floatMarketCap" | "notionalValue"
  | "capitalDeposit" | "openInterest" | "pctChange" | "amplitude" | "volume"
  | "amount" | "return3" | "return5" | "return10" | "return22" | "return44";
interface ListColumn {
  id: ListColumnKey;
  label: string;
}
interface FlagChoice {
  color: string;
  label: string;
}
type DrawingKind = ChartDrawing["type"];
interface DrawingPreferences {
  magnet: boolean;
  crossPeriod: boolean;
  keepDrawing: boolean;
  styles: Partial<Record<DrawingKind, ChartDrawingStyle>>;
}
const defaultFibonacciRetracementLevels: readonly FibonacciRetracementLevel[] = [
  { ratio: 0, label: "0.0%" },
  { ratio: 0.236, label: "23.6%" },
  { ratio: 0.382, label: "38.2%" },
  { ratio: 0.5, label: "50.0%" },
  { ratio: 0.618, label: "61.8%" },
  { ratio: 0.786, label: "78.6%" },
  { ratio: 1, label: "100.0%" },
];

const fallbackCategories: MarketCategory[] = [
  ["all", "全部市场"],
  ["exchange-index", "交易所指数"],
  ["csi-index", "中证指数"],
  ["cni-index", "国证指数"],
  ["huazheng-index", "华证指数"],
  ["tdx-index", "通达信指数"],
  ["a-sh", "A股-沪市"],
  ["a-sz", "A股-深市"],
  ["a-bse", "A股-北证"],
  ["a-chinext", "A股-创业板"],
  ["a-star", "A股-科创板"],
  ["a-etf", "A股-ETF基金"],
  ["a-convertible", "A股-可转债"],
  ["a-exchangeable", "A股-可交债"],
  ["a-pledged-repo", "A股-债券质押式回购"],
  ["a-lof", "A股-LOF基金"],
  ["a-reit", "A股-REITs"],
  ["hk-index", "港股-指数"],
  ["hk-stock", "港股-个股"],
  ["global-index", "全球-指数"],
  ["global-fx", "全球-基本汇率"],
  ["future-comex", "纽约COMEX"],
  ["future-nymex", "纽约NYMEX"],
  ["future-cbot", "芝加哥CBOT"],
  ["future-cme", "芝加哥CME"],
  ["future-ice", "洲际交易所ICE"],
  ["future-lme", "伦敦LME"],
  ["future-sgx", "新加坡SGX"],
  ["cn-macro", "中国-宏观指标"],
  ["cn-future-index", "国内期货-指数"],
  ["cn-future-main", "国内期货主连合约"],
  ["cn-future-weighted", "国内期货加权合约"],
  ["cn-future-shfe", "上海期货交易所"],
  ["cn-future-ine", "上海国际能源交易中心"],
  ["cn-future-dce", "大连商品交易所"],
  ["cn-future-czce", "郑州商品交易所"],
  ["cn-future-cffex", "中国金融期货交易所"],
  ["cn-future-gfex", "广州期货交易所"],
].map(([id, label]) => ({ id, label }));
const periodOptions = [
  ["5m", "5分"],
  ["15m", "15分"],
  ["30m", "30分"],
  ["1h", "60分"],
  ["2h", "120分"],
  ["1d", "日线"],
  ["1w", "周线"],
  ["1mo", "月线"],
  ["3mo", "季线"],
  ["1y", "年线"],
] as const;
const defaultListColumns: ListColumn[] = [
  ["name", "名称"], ["symbol", "代码"], ["latestPrice", "最新价"],
  ["totalMarketCap", "总市值"], ["floatMarketCap", "流通市值"],
  ["notionalValue", "市值"],
  ["capitalDeposit", "沉淀资金"], ["openInterest", "持仓量"], ["pctChange", "涨幅"],
  ["amplitude", "振幅"], ["volume", "成交量"], ["amount", "成交额"],
  ["return3", "近3日涨幅"], ["return5", "近5日涨幅"], ["return10", "近10日涨幅"],
  ["return22", "近22日涨幅"], ["return44", "近44日涨幅"],
].map(([id, label]) => ({ id: id as ListColumnKey, label }));
const drawingTools: Array<{ id: DrawingTool; label: string; path: string }> = [
  { id: "cursor", label: "光标", path: "M12 2v20M2 12h20M8 8l8 8M16 8l-8 8" },
  {
    id: "horizontal",
    label: "水平线",
    path: "M3 12h6M15 12h6M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  },
  {
    id: "vertical",
    label: "垂直线",
    path: "M12 3v6M12 15v6M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  },
  {
    id: "trend", label: "趋势线", path: "M4 20L20 4M2 18l4 4M18 2l4 4",
  },
  {
    id: "rectangle",
    label: "箱体线",
    path: "M4 5h16v14H4zM2 12a2 2 0 1 0 4 0a2 2 0 1 0-4 0M18 12a2 2 0 1 0 4 0a2 2 0 1 0-4 0",
  },
  {
    id: "fibonacci_retracement",
    label: "斐波回撤",
    path: "M4 5h16M4 9h12M4 13h8M4 17h4M7 4v14M17 4v4M13 8v4M9 12v4",
  },
  {
    id: "brush",
    label: "笔刷",
    path: "M5 18c4-1 5-7 9-9l3 3c-3 2-4 6-8 7H5zM16 6l2-2 3 3-2 2z",
  },
  {id: "long_position", label:"多头盈亏比", path:"M3 3h18v18H3zM3 15h18M8 10l4-5 4 5M12 5v14"},
  {id: "short_position", label:"空头盈亏比", path:"M3 3h18v18H3zM3 9h18M8 14l4 5 4-5M12 5v14"},
  { id: "text", label: "文本框", path: "M5 5h14M12 5v14M8 19h8" },
  { id: "laser", label: "激光笔", path: "M3 21l10-10M12 3v4M17 7l3-3M18 12h4M15 15l3 3M8 4l2 3" },
];
const drawingGroups = [
  {id:'lines', label:'线条工具', ids:['trend','horizontal','vertical']},
  {id:'shapes', label:'图形与盈亏比', ids:['rectangle','fibonacci_retracement','long_position','short_position']},
  {id:'pens', label:'笔刷与激光笔', ids:['brush','laser']},
];
const drawingGroupOpen = ref("");
const groupSelection = ref<Record<string,string>>({lines:'trend', shapes:'rectangle', pens:'brush'});
const chartTypeMenu = ref(false);
const chartMenuAnchor = shallowRef<HTMLElement>();
const drawingMenuAnchor = shallowRef<HTMLElement>();
function groupedTool(id: string) { return drawingTools.find((item) => item.id === groupSelection.value[id])!; }
function chooseGroupedTool(group: string, id: DrawingTool) { groupSelection.value[group] = id; drawingGroupOpen.value = ""; selectDrawingTool(id); }
const drawingColorPresets = DRAWING_COLOR_PRESETS;
const lineStyles: Array<[DrawingLineStyle, string]> = [
  ["solid", "实线"],
  ["dashed", "虚线"],
  ["dotted", "点线"],
  ["dashdot", "一长一短"],
];
const drawingPreferenceKey = "market-drawing-preferences-v1";
const flagChoices: FlagChoice[] = [
  { color: "", label: "清除标记" },
  { color: "#ef4444", label: "红色" },
  { color: "#f97316", label: "橙色" },
  { color: "#eab308", label: "黄色" },
  { color: "#22c55e", label: "绿色" },
  { color: "#3b82f6", label: "蓝色" },
  { color: "#a855f7", label: "紫色" },
];

function baseDrawingStyle(): ChartDrawingStyle {
  return {
    color: "#2196f3",
    width: 1.5,
    lineStyle: "solid",
    fillColor: "rgba(33,150,243,0.100)",
    fillOpacity: 1,
    fontSize: 14,
    borderColor: "transparent",
    borderWidth: 0,
    borderStyle: "solid",
    locked: false,
  };
}
function storedDrawingPreferences(): DrawingPreferences {
  const fallback: DrawingPreferences = {
    magnet: false,
    crossPeriod: true,
    keepDrawing: false,
    styles: {},
  };
  try {
    const value = JSON.parse(
      localStorage.getItem(drawingPreferenceKey) || "{}",
    ) as Partial<DrawingPreferences>;
    return {
      magnet:
        typeof value.magnet === "boolean" ? value.magnet : fallback.magnet,
      crossPeriod:
        typeof value.crossPeriod === "boolean"
          ? value.crossPeriod
          : fallback.crossPeriod,
      keepDrawing:
        typeof value.keepDrawing === "boolean"
          ? value.keepDrawing
          : fallback.keepDrawing,
      styles:
        value.styles && typeof value.styles === "object" ? value.styles : {},
    };
  } catch {
    return fallback;
  }
}
const storedDrawingOptions = storedDrawingPreferences();
const drawingStyleDefaults = ref<Record<DrawingKind, ChartDrawingStyle>>({
  long_position: baseDrawingStyle(),
  short_position: baseDrawingStyle(),
  trend: { ...baseDrawingStyle(), ...storedDrawingOptions.styles.trend },
  horizontal: {
    ...baseDrawingStyle(),
    ...storedDrawingOptions.styles.horizontal,
  },
  vertical: {
    ...baseDrawingStyle(),
    ...storedDrawingOptions.styles.vertical,
  },
  rectangle: {
    ...baseDrawingStyle(),
    ...storedDrawingOptions.styles.rectangle,
  },
  fibonacci_retracement: {
    ...baseDrawingStyle(),
    ...storedDrawingOptions.styles.fibonacci_retracement,
  },
  brush: { ...baseDrawingStyle(), ...storedDrawingOptions.styles.brush },
  text: { ...baseDrawingStyle(), ...storedDrawingOptions.styles.text },
});

const categories = ref<MarketCategory[]>(fallbackCategories);
const route = useRoute();
const router = useRouter();
const marketStore = useMarketStore();
const { category, query, sort: listSort } = storeToRefs(marketStore);
const allItems = ref<Instrument[]>(marketStore.items as Instrument[]);
const allTotal = ref(0);
const listElement = ref<HTMLElement>();
const toolbarElement = ref<HTMLElement>();
const toolbarHeight = ref(42);
let toolbarObserver: ResizeObserver | undefined;
const page = ref(1);
const loading = ref(false);
const usingCachedList = ref(false);
const snapshotStale = ref(false);
const error = ref("");
interface SignalEvent {instrumentId:string;strategyName:string;action:string;direction:string;at:string;barAt?:string;period?:string;price?:number}
interface MonitorResponse {events:SignalEvent[]}
const monitorEvents=ref<SignalEvent[]>([]);
const operationLabels: Record<string,string> = {open:"开仓",add:"加仓",reduce:"减仓",close:"平仓"};
const signalChartMarkers = computed<StrategyChartMarker[]>(()=>monitorEvents.value.filter(event=>event.instrumentId===selected.value?.instrumentId && event.period===history.value.period && typeof event.price==='number').map(event=>({kind:['open','add'].includes(event.action)?'entry' as const:'exit' as const,label:operationLabels[event.action],reason:event.strategyName,time:event.barAt||event.at,price:event.price!,barIndex:displayedBars.value.findIndex(bar=>Date.parse(bar.barOpenTime||bar.tradingDate||'')===Date.parse(event.barAt||event.at))})).filter(item=>item.barIndex>=0));
async function loadMonitor() { try { monitorEvents.value = ((await apiGet<MonitorResponse>("/api/composites/monitor", undefined, { force: true })).events || []).filter(event=>event.action in operationLabels); } catch { monitorEvents.value = []; } }
const selected = ref<Instrument | undefined>(
  marketStore.items.find((item) => item.instrumentId === marketStore.selectedId) as Instrument | undefined,
);
const measureEvidence = computed(() => ({
  openInterest: selected.value?.openInterest,
  fieldCapabilities: { ...selected.value?.fieldCapabilities,
    openInterest: selected.value?.fieldCapabilities?.openInterest === true || marketStore.interestInstruments.includes(selected.value?.instrumentId || "") },
}));
const fullscreen = ref(false);
const indicatorDialogOpen = ref(false);
const indicatorSearch = ref("");
const indicatorCategory = ref("all");
const indicatorFavorites = ref<string[]>(readLocal("marketlistener.strategyFavorites", []));
const indicatorSettingsId = ref("");
const chartType = ref<ChartType>((chartTypes.find((item) => item.value === localStorage.getItem("market-chart-type"))?.value) || "candles");
const replay = useChartReplay(computed(() => history.value.bars.length));
const { active: replayActive, count: replayCount, playing: replayPlaying, speed: replaySpeed, selecting: replaySelecting, following: replayFollowing } = replay;
const detailChart = ref<InstanceType<typeof KLineChart>>();
const topBoardChart = ref<InstanceType<typeof KLineChart>>();
const bottomBoardChart = ref<InstanceType<typeof KLineChart>>();
const replayViewRequest = ref<ChartRange>();
const replayVisible = ref(80);
let beforeReplayRange: ChartRange | undefined;
let previousBodyOverflow = "";
let previousRootOverflow = "";
function setDetailScrollLock(locked: boolean): void {
  if (locked) {
    previousBodyOverflow = document.body.style.overflow;
    previousRootOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    return;
  }
  document.body.style.overflow = previousBodyOverflow;
  document.documentElement.style.overflow = previousRootOverflow;
}
function readLocal<T>(key: string, fallback: T): T {
  try { const result = JSON.parse(localStorage.getItem(key) || "null"); return Array.isArray(result) ? result as T : fallback; }
  catch { return fallback; }
}
const displayedBars = computed(() => replayActive.value && !replaySelecting.value ? history.value.bars.slice(0, replayCount.value) : history.value.bars);
const filteredIndicatorCatalog = computed(() => selectableIndicatorCatalog.value.filter((item) =>
  (indicatorCategory.value === "all" || (indicatorCategory.value === "favorites" ? isIndicatorFavorite(item) : item.placement === indicatorCategory.value)) &&
  `${item.name} ${item.englishName || ""} ${item.categoryLabel || ""}`.toLowerCase().includes(indicatorSearch.value.toLowerCase().trim()),
));
function isIndicatorFavorite(item: IndicatorCatalogItem): boolean {
  return indicatorFavorites.value.includes(`${item.id}@${item.version}`) || indicatorFavorites.value.includes(item.id);
}
function toggleIndicatorFavorite(item: IndicatorCatalogItem): void {
  const key = `${item.id}@${item.version}`;
  indicatorFavorites.value = isIndicatorFavorite(item) ? indicatorFavorites.value.filter((id) => id !== key && id !== item.id) : [...indicatorFavorites.value, key];
  localStorage.setItem("marketlistener.strategyFavorites", JSON.stringify(indicatorFavorites.value));
}
function openIndicatorLibrary(): void {
  indicatorSettingsId.value = "";
  indicatorFavorites.value = readLocal("marketlistener.strategyFavorites", []);
  indicatorDialogOpen.value = true;
  void loadIndicatorCatalog();
}
function chooseCatalogIndicator(id: string): void {
  if (addIndicatorInstance(id)) { indicatorDialogOpen.value = false; void loadIndicatorSeries(); }
}
function setIndicatorCrossPeriod(instance: IndicatorInstance, value: boolean): void {
  instance.crossPeriod = value; instance.period = history.value.period; persistIndicatorInstances();
}
function stopReplay(): void {
  replay.pause();
}
function closeReplay(): void {
  if (!replayActive.value) return;
  replay.close();
  replayViewRequest.value = beforeReplayRange ? { ...beforeReplayRange } : undefined;
}
function resetReplay(): void {
  replay.close(); beforeReplayRange = undefined; replayViewRequest.value = undefined;
}
function toggleReplay(): void {
  if (replayActive.value) { closeReplay(); return; }
  beforeReplayRange = detailChart.value?.getRange();
  replayVisible.value = beforeReplayRange ? beforeReplayRange.end - beforeReplayRange.start + 1 : 80;
  replay.select();
}
function reselectReplay(): void {
  replay.select();
  replayViewRequest.value = beforeReplayRange ? { ...beforeReplayRange } : undefined;
}
watch([replayCount, replayFollowing, replaySelecting], () => {
  if (replayActive.value && !replaySelecting.value && replayFollowing.value)
    replayViewRequest.value = replayRange(replayCount.value, replayVisible.value);
});
watch([replayCount, replayActive, replaySelecting], () => {
  indicatorInstances.value = indicatorInstances.value.map((item) => ({ ...item, status: "pending", series: {}, profile: undefined }));
  detailVisibleRange.value = { start: 0, end: displayedBars.value.length - 1 };
  void loadIndicatorSeries();
});
watch(chartType, (value) => localStorage.setItem("market-chart-type", value));
watch(fullscreen, (value) => {
  setDetailScrollLock(value);
  if (!value) closeReplay();
});
function closeWorkbench(): void {
  indicatorDialogOpen.value = false; fullscreen.value = false; tool.value = "cursor";
  void router.replace({ path: "/market/all/", query: { ...route.query, category: category.value } });
}
function onWorkbenchEscape(event: KeyboardEvent): void {
  if (event.key !== "Escape" || !fullscreen.value || event.defaultPrevented || isDialogKeyboardEvent(event)) return;
  if (indicatorDialogOpen.value) return;
  if (drawingGroupOpen.value || chartTypeMenu.value) { drawingGroupOpen.value = ""; chartTypeMenu.value = false; return; }
  const editor = (event.target as HTMLElement)?.closest<HTMLElement>("input:not([readonly]), textarea, [contenteditable=true]");
  if (editor?.offsetParent && editor.closest(".workbench-overlay")) {
    editor.blur(); return;
  }
  closeWorkbench();
}
const detailLoading = ref(false);
const selectedDrawingId = ref("");
const marketVersion = ref("");
const history = ref<History>({
  total: 0,
  period: marketStore.detailPeriod,
  availablePeriods: [],
  start: 0,
  size: 0,
  bars: [],
  hasMore: false,
});
const indicatorCatalog = ref<IndicatorCatalogItem[]>([]);
const indicatorInstances = ref<IndicatorInstance[]>(storedIndicatorInstances());
const detailVisibleRange = ref({ start: 0, end: -1 });
const activeChartStrategy = ref<ActiveChartStrategy | null>(null);
const strategyBacktest = ref<StrategyBacktestResponse | null>(null);
const strategyBacktestLoading = ref(false);
const strategyBacktestError = ref("");
const strategyContractMultiplier = ref(1);
const inverse = ref(localStorage.getItem("market-chart-inverse") === "true");
const swapColors = ref(localStorage.getItem("market-chart-swap") === "true");
const drawings = ref<ChartDrawing[]>([]);
const drawingsInstrumentId = ref("");
const tool = ref<DrawingTool>("cursor");
const magnet = ref(storedDrawingOptions.magnet);
const crossPeriod = ref(storedDrawingOptions.crossPeriod);
const hiddenDrawings = ref(false);
const keepDrawing = ref(storedDrawingOptions.keepDrawing);
const emptyBoard = (period: string): Board => ({
  period,
  loadedPeriod: "",
  loadedVersion: "",
  bars: [],
  loading: false,
  error: "",
  loadingEarlier: false,
  start: 0,
  total: 0,
  hasMore: false,
  availablePeriods: [],
  requestId: 0,
  instrumentId: "",
});
const boardTop = ref<Board>(emptyBoard(storedPeriod("market-board-top", "1d")));
const boardBottom = ref<Board>(
  emptyBoard(storedPeriod("market-board-bottom", "1h")),
);
const listColumns = ref<ListColumn[]>(storedListColumns());
const draggingColumn = ref<ListColumnKey>();
const columnWidths = ref<Record<ListColumnKey, number>>(storedColumnWidths());
const rowFlags = ref<Record<string, string>>(storedRowFlags());
const flagPickerInstrumentId = ref("");
const chartWidthVw = ref(storedChartWidth());
const resizingColumn = ref<{
  id: ListColumnKey;
  startX: number;
  startWidth: number;
}>();
const resizingSplit = ref(false);
const detailEarlierLoading = ref(false);
const drawingPopoverPosition = ref<{ left: number; top: number }>();
const drawingPopoverDrag = ref<{
  pointerId: number;
  startX: number;
  startY: number;
  originLeft: number;
  originTop: number;
}>();
const workbenchChart = ref<HTMLElement>();
const drawingPopoverElement = ref<HTMLElement>();
let searchTimer: ReturnType<typeof setTimeout> | undefined;
let klineRetryTimer: ReturnType<typeof setTimeout> | undefined;
let serial = 0;
let indicatorSerial = 0;
let profileRangeTimer: ReturnType<typeof setTimeout> | undefined;
let allSerial = 0;
let drawingRevision = 0;
let historyAbort: AbortController | undefined;
let allAbort: AbortController | undefined;
let backgroundCatalogStarted = false;
let drawingSaveChain: Promise<unknown> = Promise.resolve();

function retryWhenKlineIndexReady(reason: unknown): boolean {
  if (!(reason instanceof ApiRequestError && reason.code === "KLINE_INDEX_REBUILDING")) return false;
  if (klineRetryTimer) return true;
  klineRetryTimer = setTimeout(() => {
    klineRetryTimer = undefined;
    if (!selected.value) return;
    void loadBoards();
    if (fullscreen.value) void loadHistory();
  }, reason.retryAfterMs ?? 5_000);
  return true;
}

const visibleDrawings = computed(() =>
  drawings.value
    .filter((item) => item.crossPeriod || item.period === history.value.period)
    .map((item) => ({ ...item, hidden: hiddenDrawings.value || item.hidden })),
);
const selectedDrawing = computed(() =>
  drawings.value.find((item) => item.id === selectedDrawingId.value),
);
const pageSize = 500;
// The API sorts the complete versioned snapshot before pagination. Never sort
// the partially loaded collection, which would masquerade as a global ranking.
const sortedItems = computed(() => allItems.value);
const categoryLabel = computed(() => categories.value.find(item => item.id === category.value)?.label || "全部行情");
const listCountLabel = computed(() => allTotal.value > allItems.value.length
  ? `${allItems.value.length.toLocaleString("zh-CN")} / ${allTotal.value.toLocaleString("zh-CN")}`
  : allItems.value.length.toLocaleString("zh-CN"));
const listStatus = computed(() => error.value ? "加载异常" : (usingCachedList.value || snapshotStale.value) ? "快照核对中"
  : loading.value ? "目录补齐中" : "行情快照");
const listProgress = computed(() => allTotal.value > 0 ? Math.min(100, allItems.value.length / allTotal.value * 100) : 0);
const futuresList = computed(() => category.value.startsWith("cn-future-") || category.value.startsWith("future-")
  || (allItems.value.length > 0 && allItems.value.every(item => item.assetType === "FUTURE")));
const visibleListColumns = computed(() => {
  const columns = listColumns.value.filter(column => futuresList.value
    ? !["totalMarketCap", "floatMarketCap"].includes(column.id)
    : column.id !== "notionalValue" || category.value === "all");
  if (!futuresList.value) return columns;
  const finance: ListColumnKey[] = ["notionalValue", "amount", "capitalDeposit"];
  const first = columns.findIndex(column => finance.includes(column.id));
  if (first < 0) return columns;
  const group = finance.flatMap(id => columns.filter(column => column.id === id));
  const remaining = columns.filter(column => !finance.includes(column.id));
  remaining.splice(first, 0, ...group);
  return remaining;
});
// Keep the complete market collection in memory, but only mount a small window of
// rows.  Pagination is an API transport detail rather than a user interaction.
const LIST_ROW_HEIGHT = 33;
const LIST_HEADER_HEIGHT = 30;
const LIST_WINDOW_BUFFER = 18;
const listContentOffset = computed(() => toolbarHeight.value + LIST_HEADER_HEIGHT);
const listScrollTop = ref(0);
const listViewportHeight = ref(600);
const virtualStart = computed(() =>
  Math.max(0, Math.floor(Math.max(0, listScrollTop.value - listContentOffset.value) / LIST_ROW_HEIGHT) - LIST_WINDOW_BUFFER),
);
const virtualEnd = computed(() =>
  Math.min(
    sortedItems.value.length,
    Math.ceil(Math.max(0, listScrollTop.value + listViewportHeight.value - listContentOffset.value) / LIST_ROW_HEIGHT) + LIST_WINDOW_BUFFER,
  ),
);
const virtualItems = computed(() => sortedItems.value.slice(virtualStart.value, virtualEnd.value));
const virtualTop = computed(() => virtualStart.value * LIST_ROW_HEIGHT);
const virtualBottom = computed(() =>
  Math.max(0, (sortedItems.value.length - virtualEnd.value) * LIST_ROW_HEIGHT),
);
const listGridTemplate = computed(() =>
  visibleListColumns.value.map((item) => `${columnWidths.value[item.id]}px`).join(" "),
);
const workbenchGridTemplate = computed(
  () => `minmax(0, 1fr) 8px minmax(0, ${chartWidthVw.value}vw)`,
);
const drawingPopoverStyle = computed(() =>
  drawingPopoverPosition.value
    ? {
        left: `${drawingPopoverPosition.value.left}px`,
        top: `${drawingPopoverPosition.value.top}px`,
        transform: "none",
      }
    : undefined,
);
function storedIndicatorInstances(): IndicatorInstance[] {
  try {
    const value = JSON.parse(
      localStorage.getItem("marketlistener.chartIndicatorInstances.v1") || "[]",
    ) as unknown;
    if (!Array.isArray(value)) return [];
    return value
      .filter((item): item is IndicatorInstance => {
        if (!item || typeof item !== "object") return false;
        const candidate = item as Partial<IndicatorInstance>;
        return (
          typeof candidate.instanceId === "string" &&
          typeof candidate.definitionId === "string" &&
          typeof candidate.version === "number" &&
          (candidate.placement === "overlay" || candidate.placement === "pane")
        );
      })
      .slice(0, 12)
      .map((item) => ({ ...item, status: "pending", series: {} }));
  } catch {
    return [];
  }
}
function persistIndicatorInstances(): void {
  const payload = indicatorInstances.value.map(
    ({
      series: _series,
      status: _status,
      unavailableCode: _code,
      unavailableReason: _reason,
      plots: _plots,
      profile: _profile,
      external: _external,
      ...item
    }) => item,
  );
  localStorage.setItem(
    "marketlistener.chartIndicatorInstances.v1",
    JSON.stringify(payload),
  );
}
function indicatorDefinition(
  instance: IndicatorInstance,
): IndicatorCatalogItem | undefined {
  return indicatorCatalog.value.find(
    (item) =>
      item.id === instance.definitionId && item.version === instance.version,
  );
}
function indicatorSupportsSelectedInstrument(
  definition: IndicatorCatalogItem,
): boolean {
  const assetType = selected.value?.assetType;
  return (
    !assetType ||
    !definition.supportedAssetTypes?.length ||
    definition.supportedAssetTypes.includes(assetType)
  );
}
const selectableIndicatorCatalog = computed(() =>
  indicatorCatalog.value.filter(indicatorSupportsSelectedInstrument),
);
function indicatorName(instance: IndicatorInstance): string {
  return (
    instance.displayName ||
    indicatorDefinition(instance)?.name ||
    instance.definitionId
  );
}
function externalIndicatorSource(instance: IndicatorInstance): string {
  return instance.external?.source || "本地标准序列未就绪";
}
function externalIndicatorAsOfDate(instance: IndicatorInstance): string {
  return instance.external?.asOfDate || "无";
}
function defaultIndicatorStyle(index: number): IndicatorInstance["style"] {
  const colors = [
    "#f59e0b",
    "#3b82f6",
    "#a855f7",
    "#22c55e",
    "#ef4444",
    "#06b6d4",
    "#ec4899",
  ];
  return {
    color: colors[index % colors.length],
    lineWidth: 1.5,
    lineType: "solid",
    plotStyles: {},
  };
}
async function loadIndicatorCatalog(): Promise<void> {
  try {
    indicatorCatalog.value = (
      await apiGet<{ items: IndicatorCatalogItem[] }>(
        "/api/strategy/indicators",
      )
    ).items.filter(
      (item) =>
        item.resourceKind === "indicator" && item.id.startsWith("indicator."),
    );
  } catch {
    indicatorCatalog.value = [];
  }
}
function addIndicatorInstance(
  definitionId: string,
  seed?: Partial<IndicatorInstance>,
): boolean {
  if (indicatorInstances.value.length >= 12) {
    error.value = "当前图表最多添加 12 个指标实例";
    return false;
  }
  const definition = indicatorCatalog.value.find(
    (item) =>
      item.id === definitionId &&
      (!seed?.version || item.version === seed.version),
  );
  if (!definition) {
    error.value = `未找到指标定义：${definitionId}`;
    return false;
  }
  if (definition.status !== "active") {
    error.value = definition.unavailableReason || "该指标当前不可用";
    return false;
  }
  if (!indicatorSupportsSelectedInstrument(definition)) {
    error.value = `该指标不支持当前标的资产类型：${selected.value?.assetType || "未知"}`;
    return false;
  }
  if (
    definition.id === "indicator.volume_profile" &&
    indicatorInstances.value.some(
      (item) => item.definitionId === "indicator.volume_profile",
    )
  ) {
    error.value = "当前图表最多添加一个成交量分布实例";
    return false;
  }
  const parameters = Object.fromEntries(
    definition.parameters.map((item) => [item.name, item.default]),
  );
  indicatorInstances.value = [
    ...indicatorInstances.value,
    {
      instanceId: seed?.instanceId || `indicator-${crypto.randomUUID()}`,
      definitionId: definition.id,
      version: seed?.version || definition.version,
      displayName: definition.name,
      parameters: { ...parameters, ...(seed?.parameters || {}) },
      style: {
        ...defaultIndicatorStyle(indicatorInstances.value.length),
        ...(seed?.style || {}),
      },
      visible: seed?.visible ?? true,
      crossPeriod: seed?.crossPeriod ?? true,
      period: seed?.period || history.value.period,
      placement: seed?.placement || definition.placement,
      status: "pending",
      series: {},
    },
  ];
  persistIndicatorInstances();
  return true;
}
function removeIndicator(instanceId: string): void {
  indicatorInstances.value = indicatorInstances.value.filter(
    (item) => item.instanceId !== instanceId,
  );
  persistIndicatorInstances();
}
function toggleIndicatorVisibility(instanceId: string): void {
  let becameVisible = false;
  indicatorInstances.value = indicatorInstances.value.map((item) => {
    if (item.instanceId !== instanceId) return item;
    becameVisible = !item.visible;
    return { ...item, visible: !item.visible };
  });
  persistIndicatorInstances();
  if (becameVisible && history.value.bars.length) void loadIndicatorSeries();
}
function updateIndicatorParameter(
  instanceId: string,
  name: string,
  value: number | undefined,
): void {
  if (typeof value !== "number" || !Number.isFinite(value)) return;
  indicatorInstances.value = indicatorInstances.value.map((item) =>
    item.instanceId === instanceId
      ? {
          ...item,
          status: "pending",
          parameters: { ...item.parameters, [name]: value },
        }
      : item,
  );
  persistIndicatorInstances();
  if (history.value.bars.length) void loadIndicatorSeries();
}
function updateIndicatorStyle(
  instanceId: string,
  patch: Partial<IndicatorInstance["style"]>,
): void {
  indicatorInstances.value = indicatorInstances.value.map((item) =>
    item.instanceId === instanceId
      ? { ...item, style: { ...item.style, ...patch } }
      : item,
  );
  persistIndicatorInstances();
}
const activeVolumeProfile = computed<VolumeProfile | undefined>(() =>
  indicatorInstances.value.find(
    (item) =>
      item.definitionId === "indicator.volume_profile" &&
      item.visible &&
      (item.crossPeriod !== false || item.period === history.value.period) &&
      item.status === "ready" &&
      item.profile,
  )?.profile,
);
function onDetailVisibleRange(start: number, end: number): void {
  if (start < 0 || end < start || end >= history.value.bars.length) return;
  if (
    detailVisibleRange.value.start === start &&
    detailVisibleRange.value.end === end
  )
    return;
  detailVisibleRange.value = { start, end };
  if (
    !indicatorInstances.value.some(
      (item) => item.definitionId === "indicator.volume_profile" && item.visible,
    )
  )
    return;
  if (profileRangeTimer) clearTimeout(profileRangeTimer);
  profileRangeTimer = setTimeout(() => {
    profileRangeTimer = undefined;
    void loadIndicatorSeries();
  }, 120);
}
function consumePendingIndicator(): boolean {
  let pending: Partial<IndicatorInstance> & { definitionId?: string; transferId?: string } = {};
  try {
    pending = JSON.parse(
      localStorage.getItem("marketlistener.pendingIndicator") || "{}",
    ) as typeof pending;
  } catch {
    pending = {};
  }
  const definitionId =
    pending.definitionId ||
    (typeof route.query.indicator === "string" ? route.query.indicator : "");
  if (!definitionId) return false;
  const transferId = pending.transferId ||
    (typeof route.query.transferId === "string" ? route.query.transferId : "");
  const transferKey = "marketlistener.lastIndicatorTransfer";
  localStorage.removeItem("marketlistener.pendingIndicator");
  const nextQuery = { ...route.query };
  delete nextQuery.indicator;
  delete nextQuery.version;
  delete nextQuery.transferId;
  void router.replace({ query: nextQuery });
  if (transferId && sessionStorage.getItem(transferKey) === transferId) return false;
  const added = addIndicatorInstance(definitionId, {
    ...pending,
    version: Number(pending.version || route.query.version || 1),
  });
  if (transferId && added) sessionStorage.setItem(transferKey, transferId);
  return added;
}
function consumePendingStrategy(): boolean {
  let pending: Partial<ActiveChartStrategy> = {};
  try {
    pending = JSON.parse(
      localStorage.getItem("marketlistener.pendingStrategy") || "{}",
    ) as typeof pending;
  } catch {
    pending = {};
  }
  const strategyId =
    pending.strategyId ||
    (typeof route.query.strategy === "string" ? route.query.strategy : "");
  if (!strategyId) return false;
  activeChartStrategy.value = {
    strategyId,
    strategyVersion: Number(
      pending.strategyVersion || route.query.version || 1,
    ),
    displayName: pending.displayName || strategyId,
    parameters: pending.parameters || {},
  };
  strategyBacktest.value = null;
  localStorage.removeItem("marketlistener.pendingStrategy");
  const nextQuery = { ...route.query };
  delete nextQuery.strategy;
  delete nextQuery.version;
  void router.replace({ query: nextQuery });
  return true;
}
function consumePendingDrawingTool(): DrawingTool | null {
  let pendingTool = "";
  try {
    const pending = JSON.parse(
      localStorage.getItem("marketlistener.pendingDrawingTool") || "{}",
    ) as { tool?: unknown };
    pendingTool = typeof pending.tool === "string" ? pending.tool : "";
  } catch {
    pendingTool = "";
  }
  const routeTool =
    typeof route.query.drawingTool === "string"
      ? route.query.drawingTool
      : "";
  const toolId = pendingTool || routeTool;
  if (toolId !== "fibonacci_retracement") return null;
  localStorage.removeItem("marketlistener.pendingDrawingTool");
  const nextQuery = { ...route.query };
  delete nextQuery.drawingTool;
  void router.replace({ query: nextQuery });
  return toolId;
}
async function runActiveStrategyBacktest(): Promise<void> {
  if (!selected.value || !activeChartStrategy.value) return;
  const instrumentId = selected.value.instrumentId;
  const strategy = activeChartStrategy.value;
  strategyBacktestLoading.value = true;
  strategyBacktestError.value = "";
  try {
    const result = await apiPost<StrategyBacktestResponse>(
      "/api/strategy/backtests",
      {
        strategyId: strategy.strategyId,
        strategyVersion: strategy.strategyVersion,
        instrumentId,
        parameters: strategy.parameters,
        limit: 5000,
        contractMultiplier: strategyContractMultiplier.value,
        currency: "CNY",
      },
    );
    if (
      selected.value?.instrumentId === instrumentId &&
      activeChartStrategy.value?.strategyId === strategy.strategyId
    )
      strategyBacktest.value = result;
  } catch (reason) {
    strategyBacktest.value = null;
    strategyBacktestError.value =
      reason instanceof Error ? reason.message : "策略回测失败";
  } finally {
    strategyBacktestLoading.value = false;
  }
}
async function reproduceStrategyBacktest(): Promise<void> {
  const current = strategyBacktest.value;
  if (!current) return;
  strategyBacktestLoading.value = true;
  strategyBacktestError.value = "";
  try {
    const response = await apiPost<{ reproduced: boolean; result: StrategyBacktestResponse }>(
      `/api/strategy/backtests/${encodeURIComponent(current.runId)}/reproduce`,
      {},
    );
    if (response.reproduced) strategyBacktest.value = response.result;
  } catch (reason) {
    strategyBacktestError.value =
      reason instanceof Error ? reason.message : "历史回测复现失败";
  } finally {
    strategyBacktestLoading.value = false;
  }
}
function updateStrategyParameter(name: string, value: unknown): void {
  if (!activeChartStrategy.value || typeof value !== "number") return;
  activeChartStrategy.value = {
    ...activeChartStrategy.value,
    parameters: { ...activeChartStrategy.value.parameters, [name]: value },
  };
  void runActiveStrategyBacktest();
}
const visibleStrategyMarkers = computed<StrategyChartMarker[]>(() => {
  const result = strategyBacktest.value;
  if (!result) return [];
  const windowStart = Math.max(0, result.barsCount - history.value.bars.length);
  return result.report.markers
    .map((item) => ({ ...item, barIndex: item.barIndex - windowStart }))
    .filter(
      (item) =>
        item.barIndex >= 0 && item.barIndex < history.value.bars.length,
    );
});
function reportMetric(name: string, digits = 2): string {
  const value = strategyBacktest.value?.report.metrics[name];
  return typeof value === "number" ? formatNumber(value, digits) : "—";
}
function clearActiveStrategy(): void {
  activeChartStrategy.value = null;
  strategyBacktest.value = null;
  strategyBacktestError.value = "";
}
function storedPeriod(key: string, fallback: string): string {
  const value = localStorage.getItem(key) || fallback;
  return periodOptions.some(([period]) => period === value) ? value : fallback;
}
function storedListColumns(): ListColumn[] {
  try {
    const ids = JSON.parse(
      localStorage.getItem("market-list-columns") || "[]",
    ) as ListColumnKey[];
    const known = [...new Set(ids)].filter(id => defaultListColumns.some(item => item.id === id));
    return known.length
      ? [...known.map(id => defaultListColumns.find(item => item.id === id)!), ...defaultListColumns.filter(item => !known.includes(item.id))]
      : [...defaultListColumns];
  } catch {
    return [...defaultListColumns];
  }
}
function storedColumnWidths(): Record<ListColumnKey, number> {
  const defaults: Record<ListColumnKey, number> = {
    symbol: 94, name: 132, latestPrice: 94, totalMarketCap: 108,
    floatMarketCap: 108, notionalValue: 108, capitalDeposit: 108, openInterest: 96,
    pctChange: 82, amplitude: 82, volume: 102, amount: 102,
    return3: 92, return5: 92, return10: 98, return22: 98, return44: 98,
  };
  try {
    const stored = JSON.parse(
      localStorage.getItem("market-list-column-widths") || "{}",
    ) as Partial<Record<ListColumnKey, number>>;
    for (const key of Object.keys(defaults) as ListColumnKey[])
      if (typeof stored[key] === "number")
        defaults[key] = Math.max(64, Math.min(360, stored[key]!));
  } catch {
    /* 使用默认列宽 */
  }
  return defaults;
}
function storedRowFlags(): Record<string, string> {
  try {
    const stored = JSON.parse(
      localStorage.getItem("market-list-row-flags") || "{}",
    ) as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries(stored).filter(
        (entry): entry is [string, string] =>
          typeof entry[1] === "string" &&
          flagChoices.some((choice) => choice.color === entry[1]),
      ),
    );
  } catch {
    return {};
  }
}
function storedChartWidth(): number {
  const stored = localStorage.getItem("market-list-chart-width-vw");
  const width = stored == null ? 50 : Number(stored);
  return Number.isFinite(width) ? Math.max(25, Math.min(75, width)) : 50;
}
function quoteValue(item: Instrument, field: ListColumnKey): string {
  return formatMarketField(item, field);
}

function quoteTitle(item: Instrument, field: ListColumnKey): string | undefined {
  if (field === "name") {
    const source = item.assetType === "FUTURE" && item.sourcePolicyVersion
      ? " · 通达信量价仓＋官网成交额及结算参数" : "";
    return `${item.name || ""} · 行情 ${item.quoteAsOf || item.dailyAsOf || "—"}${source}${item.underlyingContractCode ? ` · 对应 ${item.underlyingContractCode}` : ""}${item.officialMetricsDate ? ` · 官网指标 ${item.officialMetricsDate}` : ""}`;
  }
  if (item.assetType === "FUTURE" && item.sourcePolicyVersion && field === "notionalValue") {
    if (item.comparisonStatus === "MISMATCH" && !isFiniteQuote(item.notionalValue))
      return "通达信与官网的结算价或持仓量存在差异，市值待核验。";
    return item.seriesKind === "WEIGHTED"
      ? "各月份合约分别按通达信结算价、持仓量和官网有效乘数计算后求和。"
      : "通达信结算价 × 通达信单边持仓量 × 官网有效合约乘数。";
  }
  if (item.assetType === "FUTURE" && item.sourcePolicyVersion && field === "amount") {
    return isFiniteQuote(item.amount)
      ? `交易所官网公布的同日成交额${item.officialMetricsDate ? `，日期 ${item.officialMetricsDate}` : ""}。`
      : "缺少同合约、同交易日的官网成交额。";
  }
  if (field !== "capitalDeposit" || item.assetType !== "FUTURE"
      || !["MAIN", "SECONDARY", "WEIGHTED", "CONTRACT"].includes(item.seriesKind || "")) return undefined;
  if (!isFiniteQuote(item.capitalDeposit)) return item.capitalDepositReason || "缺少同日行情或投机保证金参数";
  const sides = isFiniteQuote(item.capitalDepositLong) && isFiniteQuote(item.capitalDepositShort)
    ? `多头 ${formatQuote(item.capitalDepositLong, 2, true)}元 + 空头 ${formatQuote(item.capitalDepositShort, 2, true)}元。` : "";
  const aggregation = item.seriesKind === "WEIGHTED" ? "同品种各月份合约资金合计。" : "";
  const published = Array.isArray(item.marginPublishedOn)
    ? item.marginPublishedOn.join("、") : item.marginPublishedOn;
  return `${sides}${aggregation}按通达信结算价、持仓量及官网适用的投机多空保证金计算。${published ? `保证金参数公布日 ${published}${item.marginCarriedForward ? "（沿用最近公布）" : ""}。` : ""}${item.quoteAsOf ? `行情日期 ${item.quoteAsOf}` : ""}`;
}
function toggleListSort(field: ListColumnKey): void {
  listSort.value = nextSortState(listSort.value, field);
  const next = { ...route.query } as Record<string, string>;
  if (listSort.value.field && listSort.value.direction) {
    next.sort = listSort.value.field;
    next.direction = listSort.value.direction;
  } else { delete next.sort; delete next.direction; }
  void router.replace({ path: route.path, query: next });
  listScrollTop.value = 0;
  if (listElement.value) listElement.value.scrollTop = 0;
  resetAll();
}
function sortMark(field: ListColumnKey): string {
  return listSort.value.field === field ? (listSort.value.direction === "asc" ? "↑" : "↓") : "";
}
function reorderColumn(target: ListColumnKey): void {
  const source = draggingColumn.value;
  if (!source || source === target) return;
  const next = [...listColumns.value];
  const from = next.findIndex((item) => item.id === source);
  const to = next.findIndex((item) => item.id === target);
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  listColumns.value = next;
  localStorage.setItem(
    "market-list-columns",
    JSON.stringify(next.map((item) => item.id)),
  );
  draggingColumn.value = undefined;
}
function startColumnResize(event: PointerEvent, id: ListColumnKey): void {
  event.preventDefault();
  event.stopPropagation();
  resizingColumn.value = {
    id,
    startX: event.clientX,
    startWidth: columnWidths.value[id],
  };
  document.body.classList.add("market-resizing");
}
function resizeByKeyboard(id: ListColumnKey, direction: number): void {
  columnWidths.value = {
    ...columnWidths.value,
    [id]: Math.max(64, Math.min(360, columnWidths.value[id] + direction * 8)),
  };
  persistColumnWidths();
}
function persistColumnWidths(): void {
  localStorage.setItem(
    "market-list-column-widths",
    JSON.stringify(columnWidths.value),
  );
}
function startSplitResize(event: PointerEvent): void {
  event.preventDefault();
  resizingSplit.value = true;
  document.body.classList.add("market-resizing");
}
function resizePointer(event: PointerEvent): void {
  if (resizingColumn.value) {
    const { id, startX, startWidth } = resizingColumn.value;
    columnWidths.value = {
      ...columnWidths.value,
      [id]: Math.max(64, Math.min(360, startWidth + event.clientX - startX)),
    };
  }
  if (resizingSplit.value) {
    const pageRight = document.documentElement.clientWidth * 0.98;
    chartWidthVw.value = Math.max(
      25,
      Math.min(
        75,
        ((pageRight - event.clientX) / document.documentElement.clientWidth) *
          100,
      ),
    );
    window.dispatchEvent(new Event("resize"));
  }
}
function stopResize(): void {
  if (resizingColumn.value) persistColumnWidths();
  if (resizingSplit.value)
    localStorage.setItem(
      "market-list-chart-width-vw",
      chartWidthVw.value.toFixed(2),
    );
  resizingColumn.value = undefined;
  resizingSplit.value = false;
  document.body.classList.remove("market-resizing");
}
function resizeSplitByKeyboard(direction: number): void {
  chartWidthVw.value = Math.max(
    25,
    Math.min(75, chartWidthVw.value + direction),
  );
  localStorage.setItem(
    "market-list-chart-width-vw",
    chartWidthVw.value.toFixed(2),
  );
  window.dispatchEvent(new Event("resize"));
}
function startDrawingPopoverDrag(event: PointerEvent): void {
  const handle = event.currentTarget as HTMLElement;
  const popover = handle.closest<HTMLElement>(".drawing-popover");
  const container = popover?.parentElement;
  if (!popover || !container) return;
  event.preventDefault();
  handle.setPointerCapture(event.pointerId);
  const rect = popover.getBoundingClientRect();
  const parentRect = container.getBoundingClientRect();
  const originLeft = rect.left - parentRect.left;
  const originTop = rect.top - parentRect.top;
  drawingPopoverPosition.value = { left: originLeft, top: originTop };
  drawingPopoverDrag.value = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    originLeft,
    originTop,
  };
}
function moveDrawingPopover(event: PointerEvent): void {
  const drag = drawingPopoverDrag.value;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const handle = event.currentTarget as HTMLElement;
  const popover = handle.closest<HTMLElement>(".drawing-popover");
  const container = popover?.parentElement;
  if (!popover || !container) return;
  const left = Math.max(
    0,
    Math.min(
      container.clientWidth - popover.offsetWidth,
      drag.originLeft + event.clientX - drag.startX,
    ),
  );
  const top = Math.max(
    0,
    Math.min(
      container.clientHeight - popover.offsetHeight,
      drag.originTop + event.clientY - drag.startY,
    ),
  );
  drawingPopoverPosition.value = { left, top };
}
function stopDrawingPopoverDrag(event: PointerEvent): void {
  if (drawingPopoverDrag.value?.pointerId !== event.pointerId) return;
  const handle = event.currentTarget as HTMLElement;
  if (handle.hasPointerCapture(event.pointerId))
    handle.releasePointerCapture(event.pointerId);
  drawingPopoverDrag.value = undefined;
}
function closePreviewSelect(event: Event): void {
  (event.currentTarget as HTMLElement)
    .closest("details")
    ?.removeAttribute("open");
}
function onSelectDrawing(
  id: string,
  anchor?: { left: number; top: number },
): void {
  selectedDrawingId.value = id;
  if (!id) {
    drawingPopoverPosition.value = undefined;
    return;
  }
  if (!anchor) return;
  void nextTick(() => {
    const popover = drawingPopoverElement.value;
    const container = workbenchChart.value;
    if (!popover || !container) return;
    const width = popover.offsetWidth || 320;
    const left = Math.max(
      0,
      Math.min(anchor.left - width / 2, container.clientWidth - width),
    );
    const below = anchor.top + 12;
    const above = anchor.top - popover.offsetHeight - 12;
    const preferredTop =
      below + popover.offsetHeight <= container.clientHeight || above < 0
        ? below
        : above;
    const top = Math.max(
      0,
      Math.min(preferredTop, container.clientHeight - popover.offsetHeight),
    );
    drawingPopoverPosition.value = { left, top };
  });
}
function selectDrawingTool(next: DrawingTool): void {
  tool.value = next;
  selectedDrawingId.value = "";
  drawingPopoverPosition.value = undefined;
}
function dismissDrawingPopover(event: PointerEvent): void {
  const target = event.target as HTMLElement | null;
  if (
    target?.closest(".drawing-popover") ||
    target?.closest(".drawing-toolbar") ||
    target?.closest(".workbench-chart")
  )
    return;
  onSelectDrawing("");
}

async function loadMarketVersion(): Promise<void> {
  try {
    const version = (
      await apiGet<{ dataVersion: string }>(
        "/api/market/cache-status",
        undefined,
        { ttlMs: 0, persist: false, force: true },
      )
    ).dataVersion;
    if (!marketVersion.value) marketVersion.value = version;
  } catch {
    // A list response carries its own authoritative version.
  }
}
async function loadCategories(): Promise<void> {
  try {
    categories.value = (
      await apiGet<{ items: MarketCategory[] }>(
        "/api/market/categories",
        undefined,
        { ttlMs: 60 * 60_000, persist: true },
      )
    ).items;
  } catch {
    categories.value = fallbackCategories;
  }
  if (!categories.value.some(item => item.id === category.value)) {
    category.value = DEFAULT_MARKET_CATEGORY;
    resetAll();
  }
}
function initializeRouteFilters(): void {
  const requested = typeof route.query.category === "string" ? route.query.category : "";
  query.value = typeof route.query.q === "string" ? route.query.q : query.value;
  const requestedSort = typeof route.query.sort === "string" ? route.query.sort : "";
  const requestedDirection = route.query.direction === "asc" || route.query.direction === "desc" ? route.query.direction : null;
  listSort.value = defaultListColumns.some((item) => item.id === requestedSort) && requestedDirection
    ? { field: requestedSort, direction: requestedDirection }
    : { field: null, direction: null };
  category.value = categories.value.some((item) => item.id === requested)
    ? requested
    : DEFAULT_MARKET_CATEGORY;
  if (requested !== category.value) {
    const path = route.path.includes("/instrument/")
      ? route.path
      : route.path.includes("/targets/") ? "/market/targets/" : "/market/all/";
    void router.replace({ path, query: { ...route.query, category: category.value } });
  }
}
interface MarketPageResponse { items: Instrument[]; total: number; dataVersion?: string; snapshotStatus?: "CURRENT" | "STALE" }
async function loadAll(retryVersion = true): Promise<void> {
  if (listSort.value.field && !visibleListColumns.value.some(column => column.id === listSort.value.field))
    listSort.value = { field: null, direction: null };
  allAbort?.abort();
  const controller = new AbortController();
  allAbort = controller;
  const request = ++allSerial;
  loading.value = true;
  usingCachedList.value = allItems.value.length > 0;
  error.value = "";
  const baseParams = { categoryKey: category.value, q: query.value,
    sortField: listSort.value.field || undefined, sortDirection: listSort.value.direction || undefined, pageSize };
  const pages = new Map<number, Instrument[]>();
  let total = 0;
  let snapshotVersion = "";
  let freshFirstPage = false;
  const publish = (): void => {
    if (request !== allSerial) return;
    const received = new Map<string, Instrument>();
    let lastPage = 0;
    while (pages.has(lastPage + 1)) {
      lastPage++;
      for (const item of pages.get(lastPage)!) received.set(item.instrumentId, item);
    }
    allItems.value = [...received.values()];
    marketStore.items = allItems.value;
    allTotal.value = total;
    page.value = lastPage || 1;
    const routeId = typeof route.params.instrumentId === "string" ? route.params.instrumentId : "";
    const wantedId = routeId || selected.value?.instrumentId || "";
    const wanted = received.get(wantedId);
    if (wanted) selected.value = wanted;
    else if (!selected.value || !wantedId) selected.value = allItems.value[0];
    else if (lastPage * pageSize >= total && !routeId) selected.value = allItems.value[0];
  };
  // Cache is a visible prefill only. The forced response below replaces it;
  // apiGet's silent stale revalidation cannot be the final list result.
  void peekCachedQuery<MarketPageResponse>("/api/market/instruments", { ...baseParams, page: 1 }).then(cached => {
    if (!cached || request !== allSerial || freshFirstPage) return;
    allItems.value = cached.items;
    allTotal.value = cached.total;
    usingCachedList.value = true;
    if (!selected.value) selected.value = cached.items[0];
  });
  try {
    const fetchPage = async (requestedPage: number): Promise<void> => {
      const data = await apiGet<MarketPageResponse>(
        "/api/market/instruments",
        {
          ...baseParams,
          page: requestedPage,
          version: requestedPage === 1 ? undefined : snapshotVersion || undefined,
        },
        { force: true, persist: true, signal: controller.signal },
      );
      if (request !== allSerial) return;
      if (requestedPage === 1) {
        total = data.total;
        snapshotVersion = data.dataVersion || "";
        if (snapshotVersion) marketVersion.value = snapshotVersion;
        snapshotStale.value = data.snapshotStatus === "STALE";
        freshFirstPage = true;
        usingCachedList.value = false;
      } else if ((snapshotVersion && data.dataVersion !== snapshotVersion) || data.total !== total) {
        throw new ApiRequestError("行情数据版本已更新，请重新加载列表", 409);
      }
      pages.set(requestedPage, data.items);
      publish();
      if (requestedPage === 1 && !backgroundCatalogStarted) {
        backgroundCatalogStarted = true;
        void loadMonitor();
      }
    };
    await fetchPage(1);
    if (request !== allSerial) return;
    const lastPage = Math.ceil(total / pageSize);
    let nextPage = 2;
    await Promise.all(Array.from({ length: Math.min(3, Math.max(0, lastPage - 1)) }, async () => {
      while (nextPage <= lastPage && request === allSerial) {
        const requestedPage = nextPage++;
        await fetchPage(requestedPage);
      }
    }));
    if (request !== allSerial) return;
    if (allItems.value.length < total) error.value = `当前市场仅取得 ${allItems.value.length} / ${total} 个唯一标的，列表未完整。`;
  } catch (reason) {
    if (request === allSerial && reason instanceof ApiRequestError && reason.status === 409 && retryVersion) {
      controller.abort();
      allItems.value = [];
      await loadAll(false);
      return;
    }
    controller.abort();
    if (
      request === allSerial &&
      !(reason instanceof DOMException && reason.name === "AbortError")
    )
      error.value = reason instanceof Error ? reason.message : "行情加载失败";
  } finally {
    if (request === allSerial) loading.value = false;
  }
}
function resetAll(): void {
  page.value = 1;
  allItems.value = [];
  allTotal.value = 0;
  void loadAll();
}
function chooseCategory(value: string): void {
  category.value = categories.value.some((item) => item.id === value)
    ? value
    : DEFAULT_MARKET_CATEGORY;
  const nextQuery = { ...route.query, category: category.value };
  void router.replace({ path: "/market/all/", query: nextQuery });
  resetAll();
}
watch(
  () => route.query.category,
  (value) => {
    const next = typeof value === "string" && categories.value.some((item) => item.id === value)
      ? value
      : DEFAULT_MARKET_CATEGORY;
    if (next !== category.value) {
      category.value = next;
      resetAll();
    }
  },
);
function search(): void {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(resetAll, 250);
}
function searchNow(): void {
  if (searchTimer) clearTimeout(searchTimer);
  void router.replace({ path: route.path, query: { ...route.query, category: category.value, ...(query.value ? { q: query.value } : {}) } });
  resetAll();
}
function choose(item: Instrument): void {
  selected.value = item;
  marketStore.select(item.instrumentId);
}
function isDialogKeyboardEvent(event: KeyboardEvent): boolean {
  const target = event.target instanceof HTMLElement ? event.target : null;
  return Boolean(target?.closest(".workspace-command-dialog, .el-dialog, [role='dialog'], [role='alertdialog']"));
}
function selectRelativeInstrument(event: KeyboardEvent): void {
  if (fullscreen.value || event.defaultPrevented || isDialogKeyboardEvent(event) || editableTarget(event.target)) return;
  if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
  const items = sortedItems.value;
  if (!items.length) return;
  event.preventDefault();
  const current = items.findIndex((item) => item.instrumentId === selected.value?.instrumentId);
  const nextIndex = Math.max(0, Math.min(items.length - 1, (current < 0 ? 0 : current) + (event.key === "ArrowDown" ? 1 : -1)));
  const next = items[nextIndex];
  if (!next) return;
  choose(next);
  const element = listElement.value;
  if (!element) return;
  const rowTop = listContentOffset.value + nextIndex * LIST_ROW_HEIGHT;
  const rowBottom = rowTop + LIST_ROW_HEIGHT;
  if (rowTop < element.scrollTop + listContentOffset.value) element.scrollTop = Math.max(0, rowTop - listContentOffset.value);
  else if (rowBottom > element.scrollTop + element.clientHeight) {
    element.scrollTop = Math.max(0, rowBottom - element.clientHeight);
  }
  if ((event.target as HTMLElement)?.closest(".row-main")) {
    void nextTick(() => element.querySelector<HTMLElement>(`[data-instrument-id="${CSS.escape(next.instrumentId)}"] .row-main`)?.focus({ preventScroll: true }));
  }
}
function listScroll(event: Event): void {
  const element = event.currentTarget as HTMLElement;
  listScrollTop.value = element.scrollTop;
  listViewportHeight.value = element.clientHeight;
  flagPickerInstrumentId.value = "";
}
function listWheel(event: WheelEvent): void {
  const element = event.currentTarget as HTMLElement;
  if (event.shiftKey) {
    element.scrollLeft += event.deltaY;
    return;
  }
  const multiplier =
    event.deltaMode === WheelEvent.DOM_DELTA_LINE
      ? 24
      : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
        ? element.clientHeight
        : 1;
  element.scrollTop += event.deltaY * multiplier;
}
function toggleFlagPicker(instrumentId: string): void {
  flagPickerInstrumentId.value =
    flagPickerInstrumentId.value === instrumentId ? "" : instrumentId;
  if (flagPickerInstrumentId.value) {
    void nextTick(() => listElement.value?.querySelector<HTMLElement>(
      `[data-instrument-id="${CSS.escape(instrumentId)}"] .flag-palette [aria-checked="true"]`,
    )?.focus({ preventScroll: true }));
  }
}
function focusRowFlag(instrumentId: string): void {
  void nextTick(() => listElement.value?.querySelector<HTMLElement>(
    `[data-instrument-id="${CSS.escape(instrumentId)}"] .row-flag`,
  )?.focus({ preventScroll: true }));
}
function onFlagPickerKeydown(event: KeyboardEvent, instrumentId: string): void {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    flagPickerInstrumentId.value = "";
    focusRowFlag(instrumentId);
    return;
  }
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  const menu = event.currentTarget as HTMLElement;
  const choices = Array.from(menu.querySelectorAll<HTMLElement>("button"));
  if (!choices.length) return;
  const current = choices.indexOf(document.activeElement as HTMLElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1
    : (Math.max(0, current) + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1) + choices.length) % choices.length;
  choices[next]?.focus({ preventScroll: true });
}
function closeFlagPickerWhenLeaving(event: FocusEvent): void {
  const menu = event.currentTarget as HTMLElement;
  if (event.relatedTarget === menu.parentElement?.querySelector(".row-flag")) return;
  if (!(event.relatedTarget instanceof Node) || !menu.contains(event.relatedTarget)) flagPickerInstrumentId.value = "";
}
function setRowFlag(instrumentId: string, color: string): void {
  const next = { ...rowFlags.value };
  if (color) next[instrumentId] = color;
  else delete next[instrumentId];
  rowFlags.value = next;
  flagPickerInstrumentId.value = "";
  localStorage.setItem("market-list-row-flags", JSON.stringify(next));
  focusRowFlag(instrumentId);
}
function rowFlagStyle(item: Instrument): Record<string, string> {
  const color = rowFlags.value[item.instrumentId];
  return color ? { "--row-flag-color": color } : {};
}

async function loadBoard(instrumentId: string, board: Board): Promise<void> {
  board.abort?.abort();
  const controller = new AbortController();
  board.abort = controller;
  const requestId = ++board.requestId;
  const requestedVersion = marketVersion.value;
  const keepCurrent = board.instrumentId === instrumentId && board.loadedPeriod === board.period && board.bars.length > 0;
  board.instrumentId = instrumentId;
  board.loading = true;
  board.error = "";
  board.loadingEarlier = false;
  if (!keepCurrent) {
    board.bars = [];
    board.loadedVersion = "";
    board.start = 0;
    board.total = 0;
    board.before = undefined;
    board.hasMore = false;
    board.availablePeriods = [];
  }
  try {
    const data = await apiGet<{
      bars: KLineBar[];
      start?: number;
      size?: number;
      total: number;
      historyTotal?: number;
      before?: string;
      hasMore?: boolean;
      availablePeriods: string[];
    }>(
      `/api/market/instruments/${encodeURIComponent(instrumentId)}/bars`,
      {
        period: board.period,
        limit: 60,
        version: marketVersion.value || undefined,
      },
      { ttlMs: 5 * 60_000, persist: true, signal: controller.signal },
    );
    if (requestId !== board.requestId || board.instrumentId !== instrumentId)
      return;
    const merged = keepCurrent && requestedVersion && board.loadedVersion === requestedVersion
      ? mergeBarTail(board.bars, data.bars) : null;
    board.bars = merged ?? data.bars;
    board.loadedPeriod = board.period;
    board.loadedVersion = requestedVersion;
    marketStore.observeMeasures(instrumentId, selected.value, data.bars);
    board.start = merged ? board.start : data.start ??
      Math.max(0, (data.historyTotal ?? data.total) - data.bars.length);
    board.total = data.historyTotal ?? data.total;
    board.before = merged ? board.before : data.before ?? data.bars[0]?.barOpenTime ?? data.bars[0]?.tradingDate;
    board.hasMore = merged ? board.hasMore : data.hasMore ?? board.start > 0;
    board.availablePeriods = data.availablePeriods;
  } catch (reason) {
    if (
      requestId === board.requestId &&
      !(reason instanceof DOMException && reason.name === "AbortError")
    ) {
      board.error = retryWhenKlineIndexReady(reason)
        ? "K线索引正在核验" : reason instanceof Error ? reason.message : "K线更新失败";
      if (!keepCurrent) board.bars = [];
    }
  } finally {
    if (requestId === board.requestId) board.loading = false;
  }
}
async function loadBoards(): Promise<void> {
  if (!selected.value) return;
  const instrumentId = selected.value.instrumentId;
  await Promise.all([
    loadBoard(instrumentId, boardTop.value),
    loadBoard(instrumentId, boardBottom.value),
    loadDrawings(instrumentId),
  ]);
}
function boardPeriod(which: "top" | "bottom", period: string): void {
  const board = which === "top" ? boardTop.value : boardBottom.value;
  board.period = period;
  localStorage.setItem(`market-board-${which}`, period);
  if (selected.value) void loadBoard(selected.value.instrumentId, board);
}
function boardPeriodAvailable(board: Board, period: string): boolean {
  return (
    board.availablePeriods.length === 0 ||
    board.availablePeriods.includes(period)
  );
}
async function loadEarlierBoard(board: Board): Promise<void> {
  if (
    board.loading ||
    board.loadingEarlier ||
    !board.hasMore ||
    !board.before ||
    !board.instrumentId
  )
    return;
  const requestId = board.requestId;
  const instrumentId = board.instrumentId;
  const period = board.period;
  const version = board.loadedVersion;
  const previousBefore = board.before;
  const historyPath = `/api/market/instruments/${encodeURIComponent(instrumentId)}/bars/history`;
  const historyParams = { period, before: previousBefore, size: 60, version: version || undefined };
  board.loadingEarlier = true;
  board.error = "";
  try {
    const data = await apiGet<History>(
      historyPath, historyParams, { force: true },
    );
    if (
      requestId !== board.requestId ||
      board.instrumentId !== instrumentId ||
      board.period !== period ||
      board.loadedVersion !== version ||
      marketVersion.value !== version ||
      board.before !== previousBefore
    )
      return;
    if (!data.bars.length && data.hasMore === false) {
      board.hasMore = false;
      return;
    }
    const combined = prependHistoryPage(data.bars, board.bars);
    if (!combined) {
      invalidateQuery(historyPath, historyParams);
      board.error = "更早行情分页日期异常或重复，请重试";
      return;
    }
    board.bars = combined;
    board.start = data.start;
    board.total = data.total;
    board.before =
      data.before ??
      data.bars[0]?.barOpenTime ??
      data.bars[0]?.tradingDate ??
      previousBefore;
    board.hasMore = data.hasMore ?? data.start > 0;
  } catch (reason) {
    if (requestId === board.requestId)
      error.value =
        reason instanceof Error ? reason.message : "更早行情加载失败";
  } finally {
    if (requestId === board.requestId) board.loadingEarlier = false;
  }
}

function saveDrawings(): Promise<unknown> {
  const instrumentId = selected.value?.instrumentId;
  if (!instrumentId) return Promise.resolve();
  const path = `/api/market/instruments/${encodeURIComponent(instrumentId)}/drawings`;
  let snapshot: ChartDrawing[];
  try {
    snapshot = structuredClone(drawings.value);
  } catch {
    snapshot = JSON.parse(JSON.stringify(drawings.value)) as ChartDrawing[];
  }
  drawingSaveChain = drawingSaveChain
    .catch(() => undefined)
    .then(async () => {
      const result = await apiPut(path, { items: snapshot });
      invalidateQuery(path);
      return result;
    })
    .catch((reason) => {
      error.value = reason instanceof Error ? reason.message : "画线保存失败";
      return undefined;
    });
  return drawingSaveChain;
}
function persistDrawingPreferences(): void {
  const styles = Object.fromEntries(
    Object.entries(drawingStyleDefaults.value).map(([type, style]) => {
      const { locked: _locked, ...persisted } = style;
      return [type, persisted];
    }),
  ) as Partial<Record<DrawingKind, ChartDrawingStyle>>;
  try {
    localStorage.setItem(
      drawingPreferenceKey,
      JSON.stringify({
        magnet: magnet.value,
        crossPeriod: crossPeriod.value,
        keepDrawing: keepDrawing.value,
        styles,
      } satisfies DrawingPreferences),
    );
  } catch {
    /* 浏览器禁用本地存储时仍保留当前会话默认值 */
  }
}
function defaultDrawingStyle(type: DrawingKind): ChartDrawingStyle {
  return {
    ...baseDrawingStyle(),
    ...drawingStyleDefaults.value[type],
    locked: false,
  };
}
function markDrawingsChanged(): void {
  drawingRevision += 1;
  if (selected.value) drawingsInstrumentId.value = selected.value.instrumentId;
}
function rememberDrawingStyle(
  type: DrawingKind,
  style?: ChartDrawingStyle,
): void {
  if (!style) return;
  const { locked: _locked, ...persisted } = {
    ...defaultDrawingStyle(type),
    ...style,
  };
  drawingStyleDefaults.value = {
    ...drawingStyleDefaults.value,
    [type]: persisted,
  };
  persistDrawingPreferences();
}
function createDrawing(
  item: Omit<ChartDrawing, "id">,
  anchor?: { left: number; top: number },
): void {
  const drawing: ChartDrawing = {
    ...item,
    id: `draw_${crypto.randomUUID()}`,
    period: history.value.period,
    crossPeriod: crossPeriod.value,
    style: defaultDrawingStyle(item.type),
  };
  drawings.value = [...drawings.value, drawing];
  markDrawingsChanged();
  onSelectDrawing(drawing.id, anchor);
  finishDrawing();
  void saveDrawings();
}
function finishDrawing(): void {
  if (!keepDrawing.value) tool.value = 'cursor';
}
function updateDrawing(item: ChartDrawing): void {
  drawings.value = drawings.value.map((drawing) =>
    drawing.id === item.id ? item : drawing,
  );
  markDrawingsChanged();
  selectedDrawingId.value = item.id;
  void saveDrawings();
}
function currentSelectedDrawing(): ChartDrawing | undefined {
  return drawings.value.find((item) => item.id === selectedDrawingId.value);
}
function patchSelectedStyle(patch: Partial<ChartDrawingStyle>): void {
  const item = currentSelectedDrawing();
  if (!item) return;
  const style = { ...defaultDrawingStyle(item.type), ...item.style, ...patch };
  if (!(Object.keys(patch).length === 1 && "locked" in patch))
    rememberDrawingStyle(item.type, style);
  updateDrawing({ ...item, style });
}
function patchSelected(
  field: "text" | "crossPeriod",
  value: string | boolean,
): void {
  const item = currentSelectedDrawing();
  if (!item) return;
  drawings.value = drawings.value.map((drawing) =>
    drawing.id === item.id ? { ...item, [field]: value } : drawing,
  );
  markDrawingsChanged();
  selectedDrawingId.value = item.id;
  if (field === "crossPeriod") {
    crossPeriod.value = Boolean(value);
    persistDrawingPreferences();
  }
  void saveDrawings();
}
function fibonacciLevelsText(item: ChartDrawing | undefined): string {
  const levels = item?.levels?.length
    ? item.levels
    : defaultFibonacciRetracementLevels;
  return levels.map((level) => String(level.ratio)).join(", ");
}
function fibonacciLevelLabel(ratio: number): string {
  const percent = Math.round(ratio * 10_000) / 100;
  return `${percent.toFixed(Number.isInteger(percent) ? 1 : 2)}%`;
}
function patchFibonacciLevels(text: string): void {
  const item = currentSelectedDrawing();
  if (!item || item.type !== "fibonacci_retracement") return;
  const ratios = text
    .split(/[，,\s]+/)
    .filter(Boolean)
    .map(Number);
  if (
    ratios.length < 2 ||
    ratios.length > 16 ||
    ratios.some((ratio) => !Number.isFinite(ratio) || ratio < -10 || ratio > 10) ||
    new Set(ratios).size !== ratios.length
  ) {
    error.value = "比例须为 2–16 个不重复的有限数值（范围 -10 至 10）";
    return;
  }
  const levels: FibonacciRetracementLevel[] = ratios.map((ratio) => ({
    ratio,
    label: fibonacciLevelLabel(ratio),
  }));
  updateDrawing({ ...item, levels });
}
function toggleDrawingPreference(
  field: "magnet" | "crossPeriod" | "keepDrawing",
): void {
  if (field === "magnet") magnet.value = !magnet.value;
  else if (field === "crossPeriod") crossPeriod.value = !crossPeriod.value;
  else keepDrawing.value = !keepDrawing.value;
  persistDrawingPreferences();
}
function deleteSelectedDrawing(): void {
  const item = currentSelectedDrawing();
  if (!item) return;
  rememberDrawingStyle(item.type, item.style);
  drawings.value = drawings.value.filter((drawing) => drawing.id !== item.id);
  markDrawingsChanged();
  selectedDrawingId.value = "";
  void saveDrawings();
}
function deleteDrawings(): void {
  for (const item of drawings.value)
    rememberDrawingStyle(item.type, item.style);
  drawings.value = [];
  markDrawingsChanged();
  selectedDrawingId.value = "";
  void saveDrawings();
}
function toggleInverse(): void {
  inverse.value = !inverse.value;
  localStorage.setItem("market-chart-inverse", String(inverse.value));
}
function toggleColors(): void {
  swapColors.value = !swapColors.value;
  localStorage.setItem("market-chart-swap", String(swapColors.value));
}

async function loadIndicatorSeries(): Promise<void> {
  const instrumentId = selected.value?.instrumentId;
  if (!instrumentId || !history.value.bars.length) return;
  if (!indicatorInstances.value.length) return;
  const request = ++indicatorSerial;
  const period = history.value.period;
  const start = history.value.start;
  const size = displayedBars.value.length;
  const replay = replayActive.value;
  const instances = indicatorInstances.value.map((item) => ({
    instanceId: item.instanceId,
    definitionId: item.definitionId,
    version: item.version,
    parameters: item.parameters,
    style: item.style,
    visible: item.visible && (item.crossPeriod !== false || item.period === period),
    placement: item.placement,
    ...(item.definitionId === "indicator.volume_profile" &&
    detailVisibleRange.value.end >= detailVisibleRange.value.start
      ? {
          rangeStart: detailVisibleRange.value.start,
          rangeEnd: Math.min(size - 1, detailVisibleRange.value.end),
        }
      : {}),
  }));
  try {
    const data = await apiPost<IndicatorSeriesResponse>(
      `/api/market/instruments/${encodeURIComponent(instrumentId)}/indicator-series`,
      { period, start, size, instances },
    );
    if (
      request !== indicatorSerial ||
      selected.value?.instrumentId !== instrumentId ||
      history.value.period !== period ||
      history.value.start !== start ||
      displayedBars.value.length !== size || replayActive.value !== replay
    )
      return;
    indicatorInstances.value = data.instances.filter((instance) => indicatorInstances.value.some((item) => item.instanceId === instance.instanceId)).map((instance) => {
      const previous = indicatorInstances.value.find((item) => item.instanceId === instance.instanceId);
      return { ...instance, style: previous?.style ?? instance.style, visible: previous?.visible ?? instance.visible, crossPeriod: previous?.crossPeriod ?? true, period: previous?.period || period };
    });
    persistIndicatorInstances();
  } catch (reason) {
    if (request === indicatorSerial)
      error.value = reason instanceof Error ? reason.message : "指标计算失败";
  }
}

async function loadHistory(): Promise<void> {
  if (!selected.value) return;
  const drawingsRevisionAtRequest = drawingRevision;
  historyAbort?.abort();
  const controller = new AbortController();
  historyAbort = controller;
  const request = ++serial;
  detailLoading.value = true;
  detailEarlierLoading.value = false;
  history.value.bars = [];
  try {
    const data = await apiGet<ChartBootstrap>(
      `/api/market/instruments/${encodeURIComponent(selected.value.instrumentId)}/chart`,
      {
        period: history.value.period,
        size: 60,
        indicators: "",
        version: marketVersion.value || undefined,
      },
      { ttlMs: 5 * 60_000, persist: true, signal: controller.signal },
    );
    if (request !== serial) return;
    marketStore.observeMeasures(selected.value.instrumentId, selected.value, data.bars);
    history.value = {
      ...data,
      before:
        data.before ?? data.bars[0]?.barOpenTime ?? data.bars[0]?.tradingDate,
      hasMore: data.hasMore ?? data.start > 0,
    };
    detailVisibleRange.value = {
      start: 0,
      end: Math.max(0, data.bars.length - 1),
    };
    if (
      drawingsInstrumentId.value !== selected.value.instrumentId &&
      drawingRevision === drawingsRevisionAtRequest
    ) {
      drawings.value = data.drawings;
      drawingsInstrumentId.value = selected.value.instrumentId;
    }
    selectedDrawingId.value = "";
    if (data.dataVersion) marketVersion.value = data.dataVersion;
    await loadIndicatorSeries();
  } catch (reason) {
    if (
      request === serial &&
      !(reason instanceof DOMException && reason.name === "AbortError")
    ) {
      if (!retryWhenKlineIndexReady(reason))
        error.value = reason instanceof Error ? reason.message : "K线加载失败";
      history.value.bars = [];
    }
  } finally {
    if (request === serial) detailLoading.value = false;
  }
}
async function loadEarlierHistory(): Promise<void> {
  if (
    !selected.value ||
    detailLoading.value ||
    detailEarlierLoading.value ||
    !history.value.hasMore ||
    !history.value.before
  )
    return;
  const request = serial;
  const instrumentId = selected.value.instrumentId;
  const period = history.value.period;
  const previousBefore = history.value.before;
  detailEarlierLoading.value = true;
  try {
    const data = await apiGet<ChartBootstrap>(
      `/api/market/instruments/${encodeURIComponent(instrumentId)}/chart`,
      {
        period,
        before: previousBefore,
        size: 60,
        indicators: "",
        version: marketVersion.value || undefined,
      },
      { ttlMs: 5 * 60_000, persist: true },
    );
    if (
      request !== serial ||
      selected.value?.instrumentId !== instrumentId ||
      history.value.period !== period ||
      history.value.before !== previousBefore
    )
      return;
    history.value = {
      ...history.value,
      start: Math.max(0, history.value.start - data.bars.length),
      size: data.bars.length + history.value.bars.length,
      total: data.total,
      before:
        data.before ??
        data.bars[0]?.barOpenTime ??
        data.bars[0]?.tradingDate ??
        previousBefore,
      hasMore: data.hasMore ?? history.value.start - data.bars.length > 0,
      earliestBarAt: data.earliestBarAt,
      bars: [...data.bars, ...history.value.bars],
    };
    await loadIndicatorSeries();
  } catch (reason) {
    if (request === serial)
      error.value =
        reason instanceof Error ? reason.message : "更早行情加载失败";
  } finally {
    if (request === serial) detailEarlierLoading.value = false;
  }
}
async function openWorkbench(item: Instrument, updateRoute = true, period = "1d"): Promise<void> {
  resetReplay();
  const changedInstrument = drawingsInstrumentId.value !== item.instrumentId;
  selected.value = item;
  fullscreen.value = true;
  if (updateRoute) {
    await router.replace({
      path: `/market/instrument/${encodeURIComponent(item.instrumentId)}/`,
      query: { ...route.query, category: category.value },
    });
  }
  history.value.period = periodOptions.some(([id]) => id === period) ? period : "1d";
  selectedDrawingId.value = "";
  if (changedInstrument) drawingsInstrumentId.value = "";
  await Promise.all([loadHistory(), loadDrawings(item.instrumentId)]);
  if (activeChartStrategy.value) await runActiveStrategyBacktest();
}
function switchPeriod(period: string): void {
  if (!marketStore.selectDetailPeriod(period, history.value.availablePeriods)) return;
  resetReplay();
  history.value.period = period;
  void loadHistory();
}

async function loadDrawings(instrumentId: string): Promise<void> {
  if (drawingsInstrumentId.value === instrumentId) return;
  const drawingsRevisionAtRequest = drawingRevision;
  // A rapid instrument round-trip can otherwise let this GET overtake an
  // already queued PUT and restore stale drawings into the reactive view.
  await drawingSaveChain.catch(() => undefined);
  try {
    const data = await apiGet<{ items: ChartDrawing[] }>(
      `/api/market/instruments/${encodeURIComponent(instrumentId)}/drawings`,
      undefined,
      { ttlMs: 5 * 60_000, persist: true, force: true },
    );
    if (
      selected.value?.instrumentId !== instrumentId ||
      drawingRevision !== drawingsRevisionAtRequest
    )
      return;
    drawings.value = data.items ?? [];
    drawingsInstrumentId.value = instrumentId;
  } catch {
    if (drawingRevision !== drawingsRevisionAtRequest) return;
    drawings.value = [];
    drawingsInstrumentId.value = instrumentId;
  }
}
function drawingsForPeriod(period: string): ChartDrawing[] {
  return drawings.value
    .filter((item) => item.crossPeriod || item.period === period)
    .map((item) => ({ ...item, hidden: hiddenDrawings.value || item.hidden }));
}
watch([() => selected.value?.instrumentId, marketVersion], () => {
  if (klineRetryTimer) clearTimeout(klineRetryTimer);
  klineRetryTimer = undefined;
  if (selected.value) marketStore.select(selected.value.instrumentId);
  void loadBoards();
  if (selected.value) void loadDrawings(selected.value.instrumentId);
});
watch(() => [selected.value?.instrumentId, history.value.period], () => {
  resetReplay();
});
onMounted(async () => {
  toolbarObserver = new ResizeObserver(() => {
    const height = Math.ceil(toolbarElement.value?.getBoundingClientRect().height || 42);
    if (height !== toolbarHeight.value) toolbarHeight.value = height;
  });
  if (toolbarElement.value) toolbarObserver.observe(toolbarElement.value);
  window.addEventListener("keydown", onWorkbenchEscape);
  window.addEventListener("keydown", selectRelativeInstrument);
  window.addEventListener("pointermove", resizePointer);
  window.addEventListener("pointerup", stopResize);
  initializeRouteFilters();
  void loadMarketVersion();
  void loadCategories();
  await loadAll();
  const routeInstrumentId = typeof route.params.instrumentId === "string" ? route.params.instrumentId : "";
  const routeInstrument = allItems.value.find((item) => item.instrumentId === routeInstrumentId);
  if (routeInstrument) await openWorkbench(routeInstrument, false);
  else if (selected.value && boardTop.value.instrumentId !== selected.value.instrumentId) await loadBoards();
  if (localStorage.getItem("marketlistener.pendingIndicator") || typeof route.query.indicator === "string")
    await loadIndicatorCatalog();
  const pendingIndicator = consumePendingIndicator();
  const pendingStrategy = consumePendingStrategy();
  const pendingDrawingTool = consumePendingDrawingTool();
  if ((pendingIndicator || pendingStrategy || pendingDrawingTool) && selected.value)
    await openWorkbench(selected.value);
  if (pendingDrawingTool) selectDrawingTool(pendingDrawingTool);
});
onBeforeUnmount(() => {
  indicatorSerial++;
  toolbarObserver?.disconnect();
  stopReplay();
  window.removeEventListener("keydown", onWorkbenchEscape);
  window.removeEventListener("keydown", selectRelativeInstrument);
  if (searchTimer) clearTimeout(searchTimer);
  if (klineRetryTimer) clearTimeout(klineRetryTimer);
  if (profileRangeTimer) clearTimeout(profileRangeTimer);
  allAbort?.abort();
  historyAbort?.abort();
  boardTop.value.abort?.abort();
  boardBottom.value.abort?.abort();
  window.removeEventListener("pointermove", resizePointer);
  window.removeEventListener("pointerup", stopResize);
  setDetailScrollLock(false);
  document.body.classList.remove("market-resizing");
});
</script>

<template>
  <main class="market-page">
    <section class="all-section">
      <header class="market-workspace-header" aria-label="行情工作台">
        <div class="workspace-heading">
          <h1>全部行情</h1>
          <span class="workspace-category" :title="categoryLabel">{{ categoryLabel }}</span>
          <span v-if="allItems.length || !loading" class="workspace-count"><b>{{ listCountLabel }}</b> 标的</span>
          <span v-else class="workspace-count-skeleton" aria-hidden="true" />
        </div>
        <div class="workspace-actions">
          <span v-if="allItems.length || !loading" class="workspace-status" :class="{ pending: loading || usingCachedList || snapshotStale, warning: Boolean(error) }" role="status"><i aria-hidden="true" />{{ listStatus }}</span>
          <button type="button" class="workspace-icon-button" aria-label="刷新行情列表" title="重新读取行情列表" :disabled="loading" @click="loadAll()">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 7A7 7 0 0 1 18 5l2 2M4 17l2 2a7 7 0 0 0 11.9-2" /></svg>
          </button>
          <button type="button" class="workspace-detail-button" :disabled="!selected" title="打开当前标的的完整K线工作台" @click="selected && openWorkbench(selected)">打开图表<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5" /></svg></button>
        </div>
        <div v-if="loading && allItems.length" class="workspace-progress" aria-hidden="true"><i :style="{ width: `${listProgress}%` }" /></div>
      </header>
      <el-alert
        v-if="error"
        :title="error"
        type="warning"
        :closable="false"
        class="page-alert"
      />
      <el-alert v-if="usingCachedList || snapshotStale" :title="loading ? '显示上次已核验行情，正在核对当前版本' : '当前版本核对未完成，暂显示上次已核验行情'" type="info" :closable="false" class="page-alert" data-test="market-cached-list" />
      <div
        class="list-workbench"
        :class="{ 'is-refreshing': loading && allItems.length > 0 }"
        :style="{ gridTemplateColumns: workbenchGridTemplate, '--market-toolbar-height': `${toolbarHeight}px` }"
      >
        <div
          ref="listElement"
          class="instrument-list"
          aria-label="行情标的列表"
          :aria-busy="loading"
          aria-describedby="market-list-keyboard-help"
          @scroll.passive="listScroll"
          @wheel.prevent="listWheel"
        >
          <div ref="toolbarElement" class="all-toolbar" aria-label="全部行情查询工具">
            <el-select :model-value="category" aria-label="市场类型" filterable placeholder="市场类型" @change="chooseCategory(String($event))"
              ><el-option v-for="item in categories" :key="item.id" :label="item.label" :value="item.id" /></el-select>
            <el-input v-model="query" aria-label="搜索全部行情" clearable placeholder="查询代码或名称" @input="search" @keyup.enter="searchNow" @clear="resetAll"><template #prefix><svg class="market-search-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></svg></template></el-input>
            <el-button type="primary" @click="searchNow">查询</el-button>
          </div>
          <p id="market-list-keyboard-help" class="market-sr-only">上下方向键切换标的，Enter打开完整K线。列标题可排序，列边界可拖动或用左右方向键调整宽度。</p>
          <div class="list-header-row">
            <span class="flag-column-title">标记</span
            ><span class="sequence-column-title">序号</span>
            <div
              class="list-table-header"
              :style="{ gridTemplateColumns: listGridTemplate }"
            >
              <div
                v-for="column in visibleListColumns"
                :key="column.id"
                class="column-header"
                :class="{ 'is-numeric': !['name', 'symbol'].includes(column.id), 'is-sorted': listSort.field === column.id, dragging: draggingColumn === column.id }"
                draggable="true"
                :title="`拖动调整${column.label}列顺序`"
                @dragstart="draggingColumn = column.id"
                @dragover.prevent
                @drop.prevent="reorderColumn(column.id)"
                @dragend="draggingColumn = undefined"
              >
                <button type="button" class="column-sort" :aria-label="`按${column.label}排序`" :aria-pressed="listSort.field === column.id" :title="listSort.field === column.id ? `${column.label}：${listSort.direction === 'desc' ? '降序' : '升序'}，再次点击切换` : `${column.label}：点击降序排列`" @click.stop="toggleListSort(column.id)"><span>{{ column.label }}</span> <b>{{ sortMark(column.id) }}</b></button>
                <button
                  type="button"
                  class="column-resizer"
                  role="separator"
                  aria-orientation="vertical"
                  :aria-label="`调整${column.label}列宽`"
                  :aria-valuenow="columnWidths[column.id]"
                  aria-valuemin="64"
                  aria-valuemax="360"
                  tabindex="0"
                  @pointerdown="startColumnResize($event, column.id)"
                  @keydown.left.prevent="resizeByKeyboard(column.id, -1)"
                  @keydown.right.prevent="resizeByKeyboard(column.id, 1)"
                />
              </div>
            </div>
            <span />
          </div>
          <div v-if="virtualTop" class="list-virtual-spacer" :style="{ height: `${virtualTop}px` }" />
          <div
            v-for="(item, index) in virtualItems"
            :key="item.instrumentId"
            class="instrument-row"
            :data-instrument-id="item.instrumentId"
            :class="{
              active: selected?.instrumentId === item.instrumentId,
              flagged: Boolean(rowFlags[item.instrumentId]),
            }"
            :style="rowFlagStyle(item)"
          >
            <div class="flag-cell">
              <button
                type="button"
                class="row-flag"
                :class="{ marked: Boolean(rowFlags[item.instrumentId]) }"
                :style="{ color: rowFlags[item.instrumentId] || undefined }"
                :aria-label="`标记${item.name || item.symbol || item.instrumentId}`"
                aria-haspopup="menu"
                :aria-expanded="flagPickerInstrumentId === item.instrumentId"
                :aria-controls="flagPickerInstrumentId === item.instrumentId ? `market-row-flag-${item.instrumentId}` : undefined"
                :title="
                  rowFlags[item.instrumentId]
                    ? '更改行标记颜色'
                    : '标记并选择行颜色'
                "
                @click.stop="toggleFlagPicker(item.instrumentId)"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M6 3v18M7 4h10l-2.3 4L17 12H7z" />
                </svg>
              </button>
              <div
                v-if="flagPickerInstrumentId === item.instrumentId"
                :id="`market-row-flag-${item.instrumentId}`"
                class="flag-palette"
                role="menu"
                aria-label="选择行标记颜色"
                @click.stop
                @keydown="onFlagPickerKeydown($event, item.instrumentId)"
                @focusout="closeFlagPickerWhenLeaving"
              >
                <button
                  v-for="choice in flagChoices"
                  :key="choice.label"
                  type="button"
                  role="menuitemradio"
                  tabindex="-1"
                  :aria-checked="(rowFlags[item.instrumentId] || '') === choice.color"
                  :aria-label="choice.label"
                  :title="choice.label"
                  :class="{ clear: !choice.color }"
                  :style="
                    choice.color ? { backgroundColor: choice.color } : undefined
                  "
                  @click="setRowFlag(item.instrumentId, choice.color)"
                >
                  {{ choice.color ? "" : "×" }}
                </button>
              </div>
            </div>
            <span class="sequence-cell">{{ virtualStart + index + 1 }}</span
            ><button
              type="button"
              class="row-main"
              :aria-pressed="selected?.instrumentId === item.instrumentId"
              :aria-label="`${item.name || item.symbol || item.instrumentId}，${item.symbol || ''}`"
              :tabindex="selected?.instrumentId === item.instrumentId || (!selected && index === 0) ? 0 : -1"
              :style="{ gridTemplateColumns: listGridTemplate }"
              title="单击切换双看板，双击打开详情 K 线"
              @click="choose(item)"
              @dblclick="openWorkbench(item)"
              @keydown.enter.prevent="openWorkbench(item)"
            >
              <span
                v-for="column in visibleListColumns"
                :key="column.id"
                :class="[`column-${column.id}`, `quote-tone-${marketFieldTone(item, column.id)}`]"
                :title="quoteTitle(item, column.id)"
                >{{ quoteValue(item, column.id) }}<small v-if="column.id === 'name' && item.underlyingContractCode" class="month-mapping"> · {{ item.underlyingContractCode }}</small></span
              ></button
            >
          </div>
          <div v-if="virtualBottom" class="list-virtual-spacer" :style="{ height: `${virtualBottom}px` }" />
          <MarketLoadingSkeleton v-if="loading" kind="list" :rows="allItems.length ? 3 : 14" />
          <div v-if="!loading && !allItems.length" class="market-list-empty">
            <svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="13" cy="13" r="8" /><path d="m19 19 8 8M9 13h8" /></svg>
            <strong>{{ error ? '行情列表暂不可用' : '没有匹配的标的' }}</strong>
            <p>{{ error ? '稍后重新读取，或切换市场类型继续查看。' : '更换市场类型，或调整代码与名称查询。' }}</p>
            <div class="market-empty-actions"><button v-if="query" type="button" @click="query = ''; searchNow()">清除搜索</button><button v-if="error || !query" type="button" @click="loadAll()">重新读取</button></div>
          </div>
        </div>
        <button
          type="button"
          class="workbench-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整行情列表与K线图宽度"
          :aria-valuenow="Math.round(chartWidthVw)"
          aria-valuemin="25"
          aria-valuemax="75"
          title="左右拖动调整K线图宽度"
          @pointerdown="startSplitResize"
          @keydown.left.prevent="resizeSplitByKeyboard(1)"
          @keydown.right.prevent="resizeSplitByKeyboard(-1)"
        >
          <span />
        </button>
        <div class="boards">
          <section class="board" aria-label="上方K线看板" :aria-busy="boardTop.loading">
            <header class="board-header">
              <div class="board-instrument"><strong>{{ selected?.symbol || '—' }}</strong><span :title="selected?.name">{{ selected?.name || '选择标的查看K线' }}</span></div>
              <div class="board-actions">
                <span v-if="boardTop.error" class="board-status" :class="{ pending: boardTop.error === 'K线索引正在核验', warning: boardTop.error !== 'K线索引正在核验' }" :title="boardTop.error" role="status">{{ boardTop.error === 'K线索引正在核验' ? '索引核验中' : boardTop.bars.length ? '保留快照' : '读取失败' }}</span>
                <i v-else-if="boardTop.loading && boardTop.bars.length" class="board-update-dot" aria-label="正在核对K线" />
                <div class="board-zoom" role="group" aria-label="上看板缩放">
                  <button type="button" aria-label="放大上方K线" title="放大K线" :disabled="boardTop.bars.length < 2" @click="topBoardChart?.zoomBoard('in')">＋</button>
                  <button type="button" aria-label="缩小上方K线" title="缩小K线；有更早历史时继续加载" :disabled="boardTop.bars.length < 2" @click="topBoardChart?.zoomBoard('out')">−</button>
                  <button type="button" aria-label="复位上方K线" title="恢复最近60根" :disabled="!boardTop.bars.length" @click="topBoardChart?.resetBoardView()">↺</button>
                </div>
                <select :value="boardTop.period" aria-label="上看板 K 线周期" @change="boardPeriod('top', ($event.target as HTMLSelectElement).value)">
                  <option v-for="[id,label] in periodOptions" :key="id" :value="id" :disabled="!boardPeriodAvailable(boardTop,id)">{{ label }}</option>
                </select>
                <button type="button" :disabled="!selected" aria-label="展开上方K线图" title="打开完整K线工作台" @click="selected && openWorkbench(selected, true, boardTop.period)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3H3v5M16 3h5v5M21 16v5h-5M3 16v5h5" /></svg></button>
              </div>
            </header>
            <MarketLoadingSkeleton v-if="(boardTop.loading && !boardTop.bars.length) || (loading && !selected)" kind="chart" />
            <KLineChart
              v-else
              ref="topBoardChart"
              :bars="boardTop.bars"
              :key="boardTop.instrumentId"
              :instrument-id="boardTop.instrumentId"
              :measure-evidence="measureEvidence"
              :period="boardTop.period"
              fill
              market-board
              :loading-earlier="boardTop.loadingEarlier"
              :has-more-history="boardTop.hasMore"
              :drawings="drawingsForPeriod(boardTop.period)"
              :total-market-cap="selected?.totalMarketCap"
              :float-market-cap="selected?.floatMarketCap"
              :future-units="selected?.assetType === 'FUTURE'"
              drawings-read-only
              open-on-double-click
              @open-detail="selected && openWorkbench(selected)"
              @request-earlier="loadEarlierBoard(boardTop)"
            />
          </section>
          <section class="board" aria-label="下方K线看板" :aria-busy="boardBottom.loading">
            <header class="board-header">
              <div class="board-instrument"><strong>{{ selected?.symbol || '—' }}</strong><span :title="selected?.name">{{ selected?.name || '选择标的查看K线' }}</span></div>
              <div class="board-actions">
                <span v-if="boardBottom.error" class="board-status" :class="{ pending: boardBottom.error === 'K线索引正在核验', warning: boardBottom.error !== 'K线索引正在核验' }" :title="boardBottom.error" role="status">{{ boardBottom.error === 'K线索引正在核验' ? '索引核验中' : boardBottom.bars.length ? '保留快照' : '读取失败' }}</span>
                <i v-else-if="boardBottom.loading && boardBottom.bars.length" class="board-update-dot" aria-label="正在核对K线" />
                <div class="board-zoom" role="group" aria-label="下看板缩放">
                  <button type="button" aria-label="放大下方K线" title="放大K线" :disabled="boardBottom.bars.length < 2" @click="bottomBoardChart?.zoomBoard('in')">＋</button>
                  <button type="button" aria-label="缩小下方K线" title="缩小K线；有更早历史时继续加载" :disabled="boardBottom.bars.length < 2" @click="bottomBoardChart?.zoomBoard('out')">−</button>
                  <button type="button" aria-label="复位下方K线" title="恢复最近60根" :disabled="!boardBottom.bars.length" @click="bottomBoardChart?.resetBoardView()">↺</button>
                </div>
                <select :value="boardBottom.period" aria-label="下看板 K 线周期" @change="boardPeriod('bottom', ($event.target as HTMLSelectElement).value)">
                  <option v-for="[id,label] in periodOptions" :key="id" :value="id" :disabled="!boardPeriodAvailable(boardBottom,id)">{{ label }}</option>
                </select>
                <button type="button" :disabled="!selected" aria-label="展开下方K线图" title="打开完整K线工作台" @click="selected && openWorkbench(selected, true, boardBottom.period)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3H3v5M16 3h5v5M21 16v5h-5M3 16v5h5" /></svg></button>
              </div>
            </header>
            <MarketLoadingSkeleton v-if="(boardBottom.loading && !boardBottom.bars.length) || (loading && !selected)" kind="chart" />
            <KLineChart
              v-else
              ref="bottomBoardChart"
              :bars="boardBottom.bars"
              :key="boardBottom.instrumentId"
              :instrument-id="boardBottom.instrumentId"
              :measure-evidence="measureEvidence"
              :period="boardBottom.period"
              fill
              market-board
              :loading-earlier="boardBottom.loadingEarlier"
              :has-more-history="boardBottom.hasMore"
              :drawings="drawingsForPeriod(boardBottom.period)"
              :total-market-cap="selected?.totalMarketCap"
              :float-market-cap="selected?.floatMarketCap"
              :future-units="selected?.assetType === 'FUTURE'"
              drawings-read-only
              open-on-double-click
              @open-detail="selected && openWorkbench(selected)"
              @request-earlier="loadEarlierBoard(boardBottom)"
            />
          </section>
        </div>
      </div>
    </section>

    <Teleport to="body"
      ><div
        v-if="fullscreen"
        class="workbench-overlay"
        @pointerdown="dismissDrawingPopover"
      >
        <header class="workbench-header">
          <div class="instrument-quote-line" aria-hidden="true" />
          <div class="workbench-actions">
            <div class="chart-type-picker">
              <button class="icon-button" aria-label="K线类型" :aria-expanded="chartTypeMenu" :title="chartTypes.find((item)=>item.value===chartType)?.label" @click="chartMenuAnchor=$event.currentTarget as HTMLElement;chartTypeMenu=!chartTypeMenu"><ChartIcon :name="chartType"/><ChartIcon name="chevron"/></button>
              <ChartMenu v-if="chartTypeMenu && chartMenuAnchor" :anchor="chartMenuAnchor" label="K线类型" :selected="chartType" :groups="chartTypeGroups.map(group=>({label:group.label,items:group.items.map(item=>({id:item.value,label:item.label}))}))" @select="chartType=$event as ChartType;chartTypeMenu=false" @close="chartTypeMenu=false" />
            </div>
            <el-button @click="openIndicatorLibrary"><ChartIcon name="indicator"/>指标</el-button>
            <el-button :type="swapColors ? 'primary' : 'default'" @click="toggleColors"><ChartIcon name="colors"/>涨跌换色</el-button>
            <el-button :type="inverse ? 'primary' : 'default'" @click="toggleInverse"><ChartIcon name="flip"/>坐标翻转</el-button>
            <el-button :type="replayActive ? 'primary' : 'default'" :disabled="history.bars.length < 2" @click="toggleReplay"><ChartIcon name="replay"/>回放</el-button>
            <el-button
            v-if="activeChartStrategy && !replayActive"
              :loading="strategyBacktestLoading"
              @click="runActiveStrategyBacktest"
              >{{ activeChartStrategy.displayName }}</el-button
            >
            <el-dialog v-model="indicatorDialogOpen" title="指标" width="860px" align-center append-to-body :z-index="3300" class="indicator-library-dialog">
              <section class="indicator-manager" aria-label="图表指标管理">
                <template v-if="!indicatorSettingsId">
                  <el-input v-model="indicatorSearch" placeholder="搜索指标名称、英文名或分类" clearable aria-label="搜索指标" />
                  <nav class="indicator-library-tabs" aria-label="指标分类">
                    <button v-for="[id, label] in [['all', '全部指标'], ['favorites', '★ 我的收藏'], ['overlay', '主图指标'], ['pane', '副图指标']]" :key="id" :class="{active: indicatorCategory === id}" @click="indicatorCategory = id">{{ label }}</button>
                  </nav>
                  <div class="indicator-library-list">
                    <div v-for="item in filteredIndicatorCatalog" :key="`${item.id}@${item.version}`" class="indicator-library-row">
                      <button class="indicator-catalog-name" :disabled="item.status !== 'active'" @click="chooseCatalogIndicator(item.id)">
                        <b>{{ item.name }}</b><small>{{ item.englishName }}</small>
                        <span>{{ item.placement === 'overlay' ? '主图' : '副图' }} · {{ item.categoryLabel }}</span>
                        <small v-if="item.status !== 'active'">{{ item.unavailableReason || '已停用' }}</small>
                      </button>
                      <button class="favorite-star" :class="{active: isIndicatorFavorite(item)}" :aria-label="`${isIndicatorFavorite(item) ? '取消收藏' : '收藏'}${item.name}`" :aria-pressed="isIndicatorFavorite(item)" @click="toggleIndicatorFavorite(item)">{{ isIndicatorFavorite(item) ? '★' : '☆' }}</button>
                    </div>
                    <p v-if="!filteredIndicatorCatalog.length">没有匹配的指标</p>
                  </div>
                </template>
                <p v-if="!selectableIndicatorCatalog.length" class="muted">
                  策略指标库中没有支持当前标的资产类型的指标。
                </p>
                <p v-if="!indicatorInstances.length" class="muted">
                  尚未添加指标。
                </p>
                <article
                  v-for="instance in indicatorInstances.filter((item) => item.instanceId === indicatorSettingsId)"
                  :key="instance.instanceId"
                  class="indicator-instance"
                  :data-instance-id="instance.instanceId"
                >
                  <header>
                    <button
                      type="button"
                      class="indicator-visibility"
                      :aria-label="
                        instance.visible
                          ? `隐藏${indicatorName(instance)}`
                          : `显示${indicatorName(instance)}`
                      "
                      @click="toggleIndicatorVisibility(instance.instanceId)"
                    >
                      {{ instance.visible ? "◉" : "○" }}</button
                    ><b>{{ indicatorName(instance) }}</b
                    ><span>{{
                      instance.placement === "overlay" ? "主图" : "副图"
                    }}</span
                    ><button
                      type="button"
                      class="indicator-remove"
                      :aria-label="`删除${indicatorName(instance)}`"
                      @click="removeIndicator(instance.instanceId)"
                    >
                      ×
                    </button>
                  </header>
                  <p
                    v-if="instance.status === 'unavailable'"
                    class="indicator-error"
                  >
                    {{ instance.unavailableReason }}
                  </p>
                  <p v-if="instance.external" class="indicator-external-meta">
                    来源：{{ externalIndicatorSource(instance) }} · 数据截至：{{
                      externalIndicatorAsOfDate(instance)
                    }} · 对齐 {{ instance.external.coverage.matchedBarCount }}/{{
                      instance.external.coverage.inputBarCount
                    }} 个交易日
                  </p>
                  <details open>
                    <summary>参数与样式</summary>
                    <div class="indicator-settings">
                      <label><span>跨周期显示</span><el-switch :model-value="instance.crossPeriod !== false" aria-label="指标跨周期" @change="setIndicatorCrossPeriod(instance, Boolean($event))" /></label>
                      <label
                        v-for="parameter in indicatorDefinition(instance)
                          ?.parameters || []"
                        :key="parameter.name"
                        ><span>{{ parameter.name }}</span
                        ><el-input-number
                          :model-value="instance.parameters[parameter.name]"
                          :min="parameter.minimum"
                          :max="parameter.maximum"
                          :step="parameter.type === 'integer' ? 1 : 0.1"
                          size="small"
                          @change="
                            updateIndicatorParameter(
                              instance.instanceId,
                              parameter.name,
                              $event,
                            )
                          " /></label
                      ><label
                        ><span>颜色</span
                        ><input
                          type="color"
                          :value="instance.style.color || '#f59e0b'"
                          @input="
                            updateIndicatorStyle(instance.instanceId, {
                              color: ($event.target as HTMLInputElement).value,
                            })
                          " /></label
                      ><label
                        ><span>线宽</span
                        ><el-input-number
                          :model-value="instance.style.lineWidth || 1.5"
                          :min="0.5"
                          :max="8"
                          :step="0.5"
                          size="small"
                          @change="
                            updateIndicatorStyle(instance.instanceId, {
                              lineWidth: Number($event),
                            })
                          " /></label
                      ><label
                        ><span>线型</span
                        ><el-select
                          :model-value="instance.style.lineType || 'solid'"
                          size="small"
                          :teleported="false"
                          @change="
                            updateIndicatorStyle(instance.instanceId, {
                              lineType: $event,
                            })
                          "
                          ><el-option label="实线" value="solid" /><el-option
                            label="虚线"
                            value="dashed" /><el-option
                            label="点线"
                            value="dotted" /></el-select
                      ></label>
                    </div>
                  </details>
                </article></section></el-dialog
            ><button
              class="close-workbench"
              type="button"
              aria-label="关闭图表"
              @click="closeWorkbench"
            >
              ×
            </button>
          </div>
        </header>
        <nav class="period-bar" aria-label="K线周期">
          <button v-for="[id, label] in periodOptions" :key="id" type="button"
            :class="{active: history.period === id, unavailable: !history.availablePeriods.includes(id)}"
            :disabled="!history.availablePeriods.includes(id)" :aria-pressed="history.period === id"
            :title="history.availablePeriods.includes(id) ? label : `${label}：当前标的暂无该周期数据`"
            @click="switchPeriod(id)">{{ label }}</button>
        </nav>
        <aside class="detail-instrument-list" aria-label="当前市场标的">
          <button
            v-for="item in sortedItems"
            :key="item.instrumentId"
            type="button"
            class="detail-instrument-row"
            :class="{active: item.instrumentId === selected?.instrumentId}"
            @click="openWorkbench(item)"
          >
            <b>{{ item.name || item.symbol || item.instrumentId }}</b>
            <small>{{ item.symbol || item.instrumentId }}</small>
            <strong :class="`quote-tone-${marketFieldTone(item, 'latestPrice')}`">{{ quoteValue(item, 'latestPrice') }}</strong>
            <em :class="`quote-tone-${marketFieldTone(item, 'pctChange')}`">{{ quoteValue(item, 'pctChange') }}</em>
          </button>
        </aside>
        <aside class="drawing-toolbar">
          <button aria-label="光标" title="光标" :class="{active:tool==='cursor'}" @click="selectDrawingTool('cursor'); drawingGroupOpen=''"><svg viewBox="0 0 24 24"><path :d="drawingTools[0].path"/></svg></button>
          <div v-for="group in drawingGroups" :key="group.id" class="drawing-tool-group">
            <button :aria-label="group.label" :title="group.label" :aria-expanded="drawingGroupOpen === group.id" :class="{active:group.ids.includes(tool)}" @click="drawingMenuAnchor=$event.currentTarget as HTMLElement;drawingGroupOpen = drawingGroupOpen === group.id ? '' : group.id">
              <svg viewBox="0 0 24 24"><path :d="groupedTool(group.id).path"/></svg><span class="group-arrow">›</span>
            </button>
            <ChartMenu v-if="drawingGroupOpen === group.id && drawingMenuAnchor" :anchor="drawingMenuAnchor" :label="group.label" :selected="tool" side="left" :groups="[{label:group.label,items:drawingTools.filter(item=>group.ids.includes(item.id)).map(item=>({...item,disabled:replayActive && item.id!=='laser'}))}]" @select="chooseGroupedTool(group.id,$event as DrawingTool)" @close="drawingGroupOpen=''" />
          </div>
          <button aria-label="文本框" title="文本框" :class="{active:tool==='text'}" :aria-pressed="tool==='text'" :disabled="replayActive" @click="selectDrawingTool('text')"><svg viewBox="0 0 24 24"><path d="M5 5h14M12 5v14M8 19h8"/></svg></button>
          <hr />
          <button
            type="button"
            :class="{ active: magnet }"
            aria-label="吸附"
            title="吸附"
            @click="toggleDrawingPreference('magnet')"
          >
            <svg viewBox="0 0 24 24">
              <g transform="rotate(-35 12 12)">
                <path
                  d="M6 3v8a6 6 0 0012 0V3h-4v8a2 2 0 01-4 0V3zM6 7h4M14 7h4"
                />
              </g>
            </svg>
          </button>
          <button
            type="button"
            :class="{ active: crossPeriod }"
            aria-label="跨周期"
            title="跨周期"
            @click="toggleDrawingPreference('crossPeriod')"
          >
            <ChartIcon name="crossPeriod" />
          </button>
          <button
            type="button"
            :class="{ active: keepDrawing }"
            aria-label="连续画线"
            title="连续画线"
            @click="toggleDrawingPreference('keepDrawing')"
          >
            <svg viewBox="0 0 24 24">
              <path d="M4 17l5-5 4 3 7-8M17 7h3v3" />
            </svg>
          </button>
          <button
            type="button"
            :class="{ active: hiddenDrawings }"
            aria-label="隐藏画线"
            title="隐藏画线"
            @click="hiddenDrawings = !hiddenDrawings"
          >
            <svg viewBox="0 0 24 24">
              <path d="M3 12s3-5 9-5 9 5 9 5-3 5-9 5-9-5-9-5z" />
              <circle cx="12" cy="12" r="2.4" />
              <path v-if="hiddenDrawings" d="M4 4l16 16" />
            </svg>
          </button>
          <button
            type="button"
            aria-label="删除全部画线"
            title="删除全部画线"
            @click="deleteDrawings"
          >
            <svg viewBox="0 0 24 24">
              <path
                d="M5 7h14M9 7V4h6v3M8 10v8M12 10v8M16 10v8M7 7l1 14h8l1-14"
              />
            </svg>
          </button>
        </aside>
        <div class="workbench-content" :class="{'replay-open':replayActive}">
          <div v-if="replayActive" class="replay-controls" aria-label="K线回放">
            <button aria-label="选择回放起点" title="选择回放起点" :class="{active:replaySelecting}" @click="reselectReplay"><ChartIcon name="start"/><span>选择K线</span></button>
            <span v-if="replaySelecting">点击图中一根 K 线开始</span>
            <button :aria-label="replayPlaying ? '暂停' : '播放'" :title="replayPlaying ? '暂停' : '播放'" :disabled="replaySelecting || replay.state.value==='ended'" @click="replay.togglePlaying"><ChartIcon :name="replayPlaying ? 'pause' : 'play'"/></button>
            <button aria-label="下一根" title="下一根" :disabled="replaySelecting || replayCount >= history.bars.length" @click="stopReplay(); replay.step()"><ChartIcon name="step"/></button>
            <button aria-label="快进10根" title="快进10根" :disabled="replaySelecting || replayCount >= history.bars.length" @click="stopReplay();replay.step(10)"><ChartIcon name="forward"/></button>
            <select v-model.number="replaySpeed" aria-label="回放速度"><option v-for="speed in REPLAY_SPEEDS" :key="speed" :value="speed">{{speed}}×</option></select>
            <span aria-label="回放周期">{{periodOptions.find(([id])=>id===history.period)?.[1]}}</span>
            <button aria-label="恢复跟随" title="恢复跟随" :aria-pressed="replayFollowing" :disabled="replaySelecting" @click="replayFollowing=true"><ChartIcon name="start"/></button>
            <span>{{ replaySelecting ? '—' : replayCount }} / {{ history.bars.length }}</span>
            <button aria-label="退出回放" title="退出回放" @click="closeReplay"><ChartIcon name="close"/></button>
          </div>
          <section
            ref="workbenchChart"
            v-loading="detailLoading"
            class="workbench-chart"
          >
          <div
            v-if="selectedDrawing"
            ref="drawingPopoverElement"
            class="drawing-popover"
            :style="drawingPopoverStyle"
          >
          <button
              class="popover-drag-handle"
              type="button"
              aria-label="拖动工具栏"
              title="拖动工具栏"
              @pointerdown.stop="startDrawingPopoverDrag"
              @pointermove="moveDrawingPopover"
              @pointerup="stopDrawingPopoverDrag"
              @pointercancel="stopDrawingPopoverDrag"
            >
              <svg viewBox="0 0 16 18">
                <circle
                  v-for="index in 6"
                  :key="index"
                  :cx="index % 2 ? 5 : 11"
                  :cy="4 + Math.floor((index - 1) / 2) * 5"
                  r="1"
                />
              </svg>
            </button>
            <DrawingColorPicker
              :model-value="selectedDrawing.style?.color || '#2196f3'"
              :title="selectedDrawing.type === 'text' ? '文字颜色' : '线条颜色'"
              :presets="drawingColorPresets"
              @update:model-value="patchSelectedStyle({ color: $event })"
            />
            <template v-if="selectedDrawing.type !== 'text'">
              <details class="preview-select" title="线宽">
                <summary aria-label="选择线宽">
                  <span
                    class="line-width-preview"
                    :style="{
                      height: `${selectedDrawing.style?.width || 1.5}px`,
                    }"
                  />
                </summary>
                <div>
                  <button
                    v-for="value in [1, 1.5, 2, 3, 4]"
                    :key="value"
                    type="button"
                    :aria-label="`${value}像素线宽`"
                    :title="`${value}像素线宽`"
                    @click="
                      patchSelectedStyle({ width: value });
                      closePreviewSelect($event);
                    "
                  >
                    <span
                      class="line-width-preview"
                      :style="{ height: `${value}px` }"
                    />
                  </button>
                </div>
              </details>
              <details class="preview-select" title="线型">
                <summary aria-label="选择线型">
                  <span
                    class="line-style-preview"
                    :class="`line-style-${selectedDrawing.style?.lineStyle || 'solid'}`"
                  />
                </summary>
                <div>
                  <button
                    v-for="[value, text] in lineStyles"
                    :key="value"
                    type="button"
                    :aria-label="text"
                    :title="text"
                    @click="
                      patchSelectedStyle({ lineStyle: value });
                      closePreviewSelect($event);
                    "
                  >
                    <span
                      class="line-style-preview"
                      :class="`line-style-${value}`"
                    />
                  </button>
                </div>
              </details>
            </template>
            <DrawingColorPicker
              v-if="selectedDrawing.type === 'rectangle'"
              :model-value="selectedDrawing.style?.fillColor || '#2196f3'"
              checkerboard
              title="箱体填充颜色"
              :presets="drawingColorPresets"
              @update:model-value="
                patchSelectedStyle({ fillColor: $event, fillOpacity: 1 })
              "
            />
            <label
              v-if="selectedDrawing.type === 'fibonacci_retracement'"
              class="fibonacci-level-input"
            >
              <span>比例</span>
              <input
                type="text"
                :value="fibonacciLevelsText(selectedDrawing)"
                aria-label="Fibonacci 比例"
                title="使用逗号分隔比例；0 为第二锚点，1 为第一锚点"
                @change="
                  patchFibonacciLevels(
                    ($event.target as HTMLInputElement).value,
                  )
                "
              />
            </label>
            <select
              v-if="selectedDrawing.type === 'text'"
              class="font-size-select"
              :value="selectedDrawing.style?.fontSize || 14"
              aria-label="字号"
              title="字号"
              @change="
                patchSelectedStyle({
                  fontSize: Number(($event.target as HTMLSelectElement).value),
                })
              "
            >
              <option
                v-for="size in [
                  10, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 56, 64, 72,
                ]"
                :key="size"
                :value="size"
              >
                {{ size }}px
              </option>
            </select>
            <button
              type="button"
              class="icon-action"
              :class="{ active: selectedDrawing.style?.locked }"
              :aria-label="selectedDrawing.style?.locked ? '解除锁定' : '锁定'"
              :title="selectedDrawing.style?.locked ? '解除锁定' : '锁定'"
              @click="
                patchSelectedStyle({ locked: !selectedDrawing.style?.locked })
              "
            >
              <svg viewBox="0 0 24 24">
                <path
                  :d="
                    selectedDrawing.style?.locked
                      ? 'M7 10V7a5 5 0 0110 0v3M6 10h12v11H6zM12 14v3'
                      : 'M8 10V7a4 4 0 018 0M6 10h12v11H6zM12 14v3'
                  "
                />
              </svg>
            </button>
            <button
              type="button"
              class="icon-action"
              :class="{ active: selectedDrawing.crossPeriod }"
              aria-label="跨周期"
              title="跨周期"
              @click="
                patchSelected('crossPeriod', !selectedDrawing.crossPeriod)
              "
          >
            <ChartIcon name="crossPeriod" />
          </button>
            <button
              type="button"
              class="icon-action danger"
              aria-label="删除"
              title="删除"
              @click="deleteSelectedDrawing"
            >
              <svg viewBox="0 0 24 24">
                <path
                  d="M5 7h14M9 7V4h6v3M8 10v8M12 10v8M16 10v8M7 7l1 14h8l1-14"
                />
              </svg>
            </button>
          </div>
          <KLineChart
            :bars="displayedBars"
            ref="detailChart"
            :replay-mode="replayActive && !replaySelecting"
            :replay-visible="replayVisible"
            :view-request="replayViewRequest"
            @browse="replayActive && (replayFollowing=false)"
            :key="selected?.instrumentId"
            :instrument-id="selected?.instrumentId"
            :measure-evidence="measureEvidence"
            :replay-pick="replayActive && replaySelecting"
            @pick-bar="replay.pick"
            :chart-type="chartType"
            :period="history.period"
            fill
            :indicator-instances="indicatorInstances.filter((item) => item.crossPeriod !== false || item.period === history.period)"
            :volume-profile="activeVolumeProfile"
            :strategy-markers="replayActive ? [] : [...visibleStrategyMarkers, ...signalChartMarkers]"
            :inverse="inverse"
            :swap-colors="swapColors"
            :drawings="replayActive ? [] : visibleDrawings"
            :drawing-tool="replayActive && tool !== 'laser' ? 'cursor' : tool"
            :brush-style="drawingStyleDefaults.brush"
            :magnet="magnet"
            :selected-drawing-id="selectedDrawingId"
            :total-market-cap="selected?.totalMarketCap"
            :float-market-cap="selected?.floatMarketCap"
            :future-units="selected?.assetType === 'FUTURE'"
            :loading-earlier="detailEarlierLoading"
            @draw="createDrawing"
            detail-layout
            @drawing-finished="finishDrawing"
            @select-drawing="onSelectDrawing"
            @update-drawing="updateDrawing"
            @visible-range="onDetailVisibleRange"
            @request-earlier="!replayActive && loadEarlierHistory()"
          >
            <template #overlay><div class="instrument-info"><strong>{{ selected?.name }}</strong><small>{{ selected?.symbol || selected?.instrumentId }}</small></div></template>
            <template #legend>
              <div class="chart-indicator-legend" aria-label="已添加指标">
                <div v-for="instance in indicatorInstances" :key="instance.instanceId" class="chart-indicator-legend-row" :class="{dimmed: !instance.visible || (instance.crossPeriod === false && instance.period !== history.period)}">
                  <span>{{ indicatorName(instance) }}</span><small>{{ instance.placement === 'overlay' ? '主图' : '副图' }}</small>
                  <button :aria-label="'跨周期'+indicatorName(instance)" title="跨周期显示" :aria-pressed="instance.crossPeriod !== false" :class="{active:instance.crossPeriod!==false}" @click="setIndicatorCrossPeriod(instance,instance.crossPeriod===false)"><ChartIcon name="crossPeriod"/></button>
                  <button :aria-label="(instance.visible?'隐藏':'显示')+indicatorName(instance)" :title="instance.visible?'隐藏指标':'显示指标'" @click="toggleIndicatorVisibility(instance.instanceId)"><ChartIcon :name="instance.visible?'visible':'hidden'"/></button>
                  <button :aria-label="'设置'+indicatorName(instance)" title="参数设置" @click="indicatorSettingsId=instance.instanceId;indicatorDialogOpen=true"><ChartIcon name="settings"/></button>
                  <button class="indicator-delete" :aria-label="'删除'+indicatorName(instance)" title="删除指标" @click="removeIndicator(instance.instanceId)">×</button>
                  <span v-if="instance.status==='unavailable'" :title="instance.unavailableReason">⚠</span>
                </div>
              </div>
            </template>
          </KLineChart>
          </section>
          <section
            v-if="activeChartStrategy && !replayActive"
            class="strategy-report"
            data-testid="strategy-report"
          >
            <header>
              <div>
                <h2>Strategy Report · {{ activeChartStrategy.displayName }}</h2>
                <p>
                  {{ activeChartStrategy.strategyId }}@{{
                    activeChartStrategy.strategyVersion
                  }} · 回测模式 · 不触达真实 Order API
                </p>
              </div>
              <div>
                <a
                  v-if="strategyBacktest"
                  class="report-export"
                  :href="`/api/strategy/backtests/${strategyBacktest.runId}/trades.csv`"
                  >导出交易 CSV</a
                >
                <el-button
                  :loading="strategyBacktestLoading"
                  @click="runActiveStrategyBacktest"
                  >重新回测</el-button
                >
                <el-button
                  v-if="strategyBacktest"
                  :loading="strategyBacktestLoading"
                  @click="void reproduceStrategyBacktest()"
                  >复现此版本</el-button
                >
                <el-button @click="clearActiveStrategy">移除策略</el-button>
              </div>
            </header>
            <div class="report-parameters">
              <label
                v-for="(value, name) in activeChartStrategy.parameters"
                :key="name"
              >
                <span>{{ name }}</span>
                <el-input-number
                  v-if="typeof value === 'number'"
                  :model-value="value"
                  :step="1"
                  @change="updateStrategyParameter(String(name), $event)"
                />
                <span v-else>{{ value }}</span>
              </label>
              <label v-if="selected?.assetType === 'FUTURE'">
                <span>合约乘数</span>
                <el-input-number
                  v-model="strategyContractMultiplier"
                  :min="0.000001"
                  @change="runActiveStrategyBacktest"
                />
              </label>
            </div>
            <el-alert
              v-if="strategyBacktestError"
              :title="strategyBacktestError"
              type="warning"
              :closable="false"
            />
            <template v-if="strategyBacktest">
              <p class="dependency-lock" data-testid="strategy-dependency-lock">
                依赖锁：策略 {{ strategyBacktest.dependencyLock.strategy.id }}@{{ strategyBacktest.dependencyLock.strategy.version }} ·
                {{ strategyBacktest.dependencyLock.strategyFunctions.length }} 个函数版本 ·
                行情 {{ strategyBacktest.dependencyLock.marketData.dataVersion }}
              </p>
              <div class="report-metrics">
                <article><span>总收益率</span><b>{{ reportMetric("totalReturnPercent") }}%</b></article>
                <article><span>年化收益率</span><b>{{ reportMetric("annualizedReturnPercent") }}%</b></article>
                <article><span>交易次数</span><b>{{ reportMetric("tradeCount", 0) }}</b></article>
                <article><span>胜率</span><b>{{ reportMetric("winRatePercent") }}%</b></article>
                <article><span>盈亏比</span><b>{{ reportMetric("averageWinLossRatio") }}</b></article>
                <article><span>Profit Factor</span><b>{{ reportMetric("profitFactor") }}</b></article>
                <article><span>最大回撤</span><b>{{ reportMetric("maxDrawdownPercent") }}%</b></article>
                <article><span>平均持仓</span><b>{{ reportMetric("averageHoldingBars") }} 根</b></article>
                <article><span>最大连赢 / 连亏</span><b>{{ reportMetric("maxConsecutiveWins", 0) }} / {{ reportMetric("maxConsecutiveLosses", 0) }}</b></article>
                <article><span>手续费</span><b>{{ reportMetric("totalCommission") }} {{ strategyBacktest.report.currency }}</b></article>
                <article><span>滑点成本</span><b>{{ reportMetric("totalSlippageCost") }} {{ strategyBacktest.report.currency }}</b></article>
                <article><span>最终权益</span><b>{{ reportMetric("finalEquity") }} {{ strategyBacktest.report.currency }}</b></article>
              </div>
              <div class="report-table-wrap">
                <table>
                  <thead><tr><th>方向</th><th>开仓时间 / 价格</th><th>平仓时间 / 价格</th><th>数量</th><th>手续费</th><th>PnL</th><th>PnL%</th><th>原因</th></tr></thead>
                  <tbody>
                    <tr v-for="trade in strategyBacktest.report.trades" :key="trade.tradeId">
                      <td>{{ trade.direction }}</td>
                      <td>{{ trade.entryTime }}<br />{{ formatNumber(trade.entryPrice, 4) }}</td>
                      <td>{{ trade.exitTime }}<br />{{ formatNumber(trade.exitPrice, 4) }}</td>
                      <td>{{ formatNumber(trade.quantity, 4) }}</td>
                      <td>{{ formatNumber(trade.commission, 2) }}</td>
                      <td :class="trade.pnl >= 0 ? 'positive' : 'negative'">{{ formatNumber(trade.pnl, 2) }}</td>
                      <td>{{ trade.pnlPercent == null ? "—" : `${formatNumber(trade.pnlPercent, 2)}%` }}</td>
                      <td>{{ trade.exitReason }}</td>
                    </tr>
                    <tr v-if="!strategyBacktest.report.trades.length"><td colspan="8">暂无已平仓交易；零分母指标保持不可用。</td></tr>
                  </tbody>
                </table>
              </div>
            </template>
          </section>
        </div></div
    ></Teleport>
  </main>
</template>

<style scoped>
.drawing-tool-group,.chart-type-picker{position:relative}
.group-arrow{position:absolute;right:1px;bottom:1px;font-size:10px}
.icon-button{display:flex;align-items:center;gap:3px;height:32px;background:transparent;color:var(--ml-text-primary);border:0;border-radius:4px;cursor:pointer}
.icon-button{border:1px solid var(--ml-divider)}
.chart-indicator-legend-row button.active{color:var(--ml-accent)}
.indicator-direct-color{width:20px;height:20px;padding:0;border:0;background:transparent;cursor:pointer}
.chart-indicator-legend-row :deep(.chart-icon){width:14px;height:14px}
.workbench-actions :deep(.chart-icon){margin-right:5px}
.workbench-header .instrument-quote-line{white-space:normal;display:flex;flex-wrap:wrap;gap:0 14px;align-content:center}
.workbench-content .replay-controls{position:fixed;bottom:36px;left:48px;right:0;z-index:20;background:var(--ml-surface)}
.replay-controls button{display:flex;align-items:center}
.replay-controls button:disabled{opacity:.35}

.drawing-group-label { font-size: 9px; color: var(--ml-text-secondary); padding-top: 3px; }
.instrument-quote-line { grid-row: 2; white-space: nowrap; overflow: hidden; font-size: 12px; gap: 12px; }
.chart-type-select { width: 145px; }
.workbench-actions :deep(.el-button + .el-button) { margin-left: 0; }
.workbench-actions .close-workbench { margin-left: auto; font-size: 26px; }
.workbench-actions { width: calc(100vw - 20px); }
.indicator-library-tabs { display: flex; gap: 8px; margin: 16px 0; }
.indicator-library-tabs button, .replay-controls button { border: 0; padding: 7px 12px; border-radius: 5px; background: var(--ml-surface-selected); color: var(--ml-text-primary); cursor: pointer; }
.indicator-library-tabs button.active { color: var(--ml-accent); font-weight: 700; }
.indicator-library-list { min-height: 360px; max-height: 55vh; overflow-y: auto; }
.indicator-library-row { display: flex; align-items: center; border-bottom: 1px solid var(--ml-divider); }
.indicator-catalog-name { flex: 1; display: grid; grid-template-columns: minmax(180px, 1fr) minmax(150px, 1fr) 130px; text-align: left; gap: 12px; align-items: center; padding: 14px 12px; border: 0; background: transparent; color: var(--ml-text-primary); cursor: pointer; }
.indicator-catalog-name:hover { background: var(--ml-surface-selected); }
.indicator-catalog-name:disabled { opacity: .5; cursor: not-allowed; }
.indicator-catalog-name small, .indicator-catalog-name span { color: var(--ml-text-secondary); }
.favorite-star { font-size: 24px; border: 0; background: transparent; color: var(--ml-text-secondary); cursor: pointer; padding: 8px 14px; }
.favorite-star.active { color: #eab308; }
.chart-indicator-legend { position: static; display: flex; flex-direction: column; align-items: flex-start; gap: 2px; max-height: 78px; overflow: auto; pointer-events: none; }
.chart-indicator-legend-row { display: flex; align-items: center; gap: 7px; background: transparent; padding: 1px 2px; text-shadow:0 1px var(--ml-background); font-size: 11px; pointer-events: none; }
.chart-indicator-legend-row small { color: var(--ml-text-secondary); }
.chart-indicator-legend-row.dimmed { opacity: .5; }
.chart-indicator-legend-row button { border: 0; color: var(--ml-text-primary); background: transparent; cursor: pointer; pointer-events:auto; }
.replay-controls { height: 40px; display: flex; align-items: center; gap: 12px; padding: 0 16px; border-bottom: 1px solid var(--ml-divider); }
.replay-controls label { display: flex; align-items: center; gap: 8px; flex: 1; }
.replay-controls input { flex: 1; }
.alert-create, .alert-row { display: flex; align-items: center; gap: 12px; margin-top: 15px; }
.alert-create :deep(.el-select) { width: 180px; }
.alert-row > span { flex: 1; }
.price-alert-toast { position: fixed; right: 20px; top: 82px; z-index: 3200; max-width: 550px; padding: 16px; background: var(--ml-surface); border: 1px solid var(--ml-accent); border-radius: 8px; box-shadow: 0 4px 20px #0003; }
.price-alert-toast button { margin-left: 12px; border: 0; background: transparent; color: inherit; cursor: pointer; }
.section-heading p {
  margin: 7px 0 0;
  color: var(--ml-text-secondary);
  font-size: 12px;
}
.market-page {
  width: 96vw;
  margin: 0 2vw;
}
.target-section,
.all-section {
  padding: 0;
  border: 0;
  background: transparent;
}
.all-section {
  margin-top: 28px;
}
.section-heading {
  margin-bottom: 14px;
}
.section-heading h1,
.section-heading h2 {
  margin: 0;
  font-size: clamp(22px, 2vw, 30px);
  letter-spacing: -0.03em;
}
.target-filters {
  display: flex;
  flex-direction: column;
  gap: 9px;
  margin-bottom: 14px;
}
.filter-row {
  display: grid;
  grid-template-columns: 42px 1fr;
  gap: 8px;
  align-items: start;
}
.filter-row > span {
  padding-top: 7px;
  color: var(--ml-text-disabled);
  font-size: 11px;
}
.target-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.target-nav button {
  border: 1px solid var(--ml-divider);
  border-radius: 6px;
  padding: 6px 10px;
  background: var(--ml-surface);
  color: var(--ml-text-secondary);
  cursor: pointer;
}
.target-nav button.active {
  border-color: var(--ml-accent);
  background: var(--ml-surface-selected);
  color: var(--ml-text-primary);
}
.target-rows {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  min-height: 42px;
}
.target-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 9px;
  align-items: center;
  min-width: 260px;
  padding: 10px 12px;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
  background: var(--ml-surface);
  color: var(--ml-text-primary);
  cursor: pointer;
  text-align: left;
}
.target-row:hover {
  border-color: var(--ml-accent);
}
.target-row span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ml-text-secondary);
}
.list-workbench {
  display: grid;
  grid-template-columns: minmax(560px, 50%) 1fr;
  height: calc(100vh - 118px);
  min-height: 600px;
  border: 1px solid var(--ml-divider);
  border-radius: 2px;
  overflow: hidden;
  background: var(--ml-surface);
}
.instrument-list {
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  border-right: 1px solid var(--ml-divider);
}
.list-header-row,
.instrument-row {
  display: grid;
  grid-template-columns: 1fr 30px;
  min-width: 560px;
  border-bottom: 1px solid var(--ml-divider);
}
.list-header-row {
  position: sticky;
  top: 0;
  z-index: 4;
  background: var(--ml-surface-elevated);
}
.list-table-header,
.row-main {
  display: grid;
  align-items: center;
  min-width: 0;
}
.instrument-row.active {
  background: var(--ml-surface-selected);
}
.row-main {
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--ml-text-primary);
  cursor: pointer;
  text-align: left;
}
.row-main > span {
  overflow: hidden;
  padding: 5px 6px;
  border-right: 1px solid color-mix(in srgb, var(--ml-divider) 60%, transparent);
  color: var(--ml-text-primary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.month-mapping { font-size: 10px; color: var(--ml-accent); }
.row-main .column-symbol,
.row-main .column-close {
  color: var(--ml-text-primary);
  font-family: ui-monospace, Consolas, monospace;
  font-weight: 700;
}
.row-detail {
  border: 0;
  background: transparent;
  color: var(--ml-text-disabled);
  cursor: pointer;
}
.row-detail:hover {
  color: var(--ml-accent);
}
.boards {
  display: grid;
  grid-template-rows: 1fr 1fr;
  min-width: 0;
  min-height: 0;
}
.board {
  min-height: 0;
  padding: 0;
  border-bottom: 1px solid var(--ml-divider);
  overflow: hidden;
}
.board:last-child {
  border-bottom: 0;
}
.period-bar button {
  flex: 0 0 auto;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: var(--ml-text-secondary);
  padding: 3px 5px;
  cursor: pointer;
  font-size: 11px;
}
.period-bar button.active {
  background: var(--ml-surface-selected);
  color: var(--ml-text-primary);
}
:global(.workbench-overlay) {
  position: fixed;
  inset: 0;
  z-index: 3000;
  background: var(--ml-background);
  color: var(--ml-text-primary);
  display: grid;
  grid-template-columns: 48px 1fr;
  grid-template-rows: 100px minmax(0, 1fr) 36px;
}
.workbench-header {
  grid-column: 1/-1;
  display: grid;
  grid-template-rows: 36px 63px;
  align-items: center;
  gap: 0;
  padding: 0 10px;
  border-bottom: 1px solid var(--ml-divider);
  background: var(--ml-surface);
}
.workbench-header > div:first-child {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.workbench-header strong {
  font-size: 14px;
}
.workbench-header span {
  overflow: hidden;
  color: var(--ml-text-secondary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.workbench-actions {
  grid-row: 1;
  display: flex;
  align-items: center;
  gap: 8px;
}
.close-workbench {
  border: 0;
  background: transparent;
  color: var(--ml-text-primary);
  font-size: 32px;
  line-height: 1;
  cursor: pointer;
}
.drawing-toolbar {
  grid-row: 2/4;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  padding: 7px 5px;
  border-right: 1px solid var(--ml-divider);
  background: var(--ml-surface);
  overflow: auto;
}
.drawing-toolbar button {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--ml-text-secondary);
  cursor: pointer;
}
.drawing-toolbar button:hover,
.drawing-toolbar button.active {
  border-color: var(--ml-accent);
  background: var(--ml-surface-selected);
  color: var(--ml-text-primary);
}
.drawing-toolbar svg {
  width: 19px;
  height: 19px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.drawing-toolbar hr {
  width: 26px;
  border: 0;
  border-top: 1px solid var(--ml-divider);
}
.period-bar {
  grid-column: 2;
  grid-row: 3;
  display: flex;
  align-items: center;
  gap: 3px;
  overflow-x: auto;
  overflow-y: hidden;
  box-sizing: border-box;
  height: 36px;
  min-height: 0;
  padding: 0 12px;
  border-bottom: 1px solid var(--ml-divider);
}
.period-bar button {
  flex: 0 0 auto;
  box-sizing: border-box;
  line-height: 18px;
  padding: 3px 9px;
}
.period-bar button.unavailable {
  opacity: 0.32;
  cursor: not-allowed;
}
.workbench-content {
  grid-column: 2;
  grid-row: 2;
  min-width: 0;
  min-height: 0;
  overflow: auto;
}
.workbench-chart {
  position: relative;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.strategy-report {
  display: grid;
  gap: 16px;
  margin: 0 16px 24px;
  padding: 18px;
  border: 1px solid var(--ml-divider);
  border-radius: 10px;
  background: var(--ml-surface);
}
.strategy-report > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.strategy-report h2,
.strategy-report p {
  margin: 0;
}
.strategy-report p {
  margin-top: 5px;
  color: var(--ml-text-secondary);
  font-size: 12px;
}
.strategy-report > header > div:last-child {
  display: flex;
  align-items: center;
  gap: 8px;
}
.report-export {
  color: var(--ml-accent);
  font-size: 13px;
  text-decoration: none;
}
.report-parameters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.report-parameters label {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--ml-text-secondary);
  font-size: 12px;
}
.report-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(120px, 1fr));
  gap: 8px;
}
.report-metrics article {
  display: grid;
  gap: 4px;
  padding: 10px;
  border: 1px solid var(--ml-divider);
  border-radius: 7px;
}
.report-metrics span {
  color: var(--ml-text-secondary);
  font-size: 11px;
}
.report-table-wrap {
  overflow: auto;
}
.report-table-wrap table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.report-table-wrap th,
.report-table-wrap td {
  padding: 8px;
  border-bottom: 1px solid var(--ml-divider);
  text-align: left;
  white-space: nowrap;
}
.report-table-wrap .positive {
  color: var(--ml-price-up);
}
.report-table-wrap .negative {
  color: var(--ml-price-down);
}
.drawing-popover {
  position: absolute;
  z-index: 12;
  top: 90px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 7px;
  max-width: calc(100% - 24px);
  padding: 6px 8px;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
  background: color-mix(in srgb, var(--ml-surface) 94%, transparent);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.24);
  overflow-x: auto;
  font-size: 11px;
}
.drawing-popover label {
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
  color: var(--ml-text-secondary);
}
.drawing-popover input[type="color"] {
  width: 26px;
  height: 24px;
  padding: 1px;
  border: 1px solid var(--ml-divider);
  background: transparent;
}
.drawing-popover input[type="text"] {
  width: 100px;
}
.drawing-popover input[type="number"] {
  width: 54px;
}
.drawing-popover select,
.drawing-popover input[type="text"],
.drawing-popover input[type="number"] {
  height: 25px;
  border: 1px solid var(--ml-divider);
  border-radius: 4px;
  background: var(--ml-background);
  color: var(--ml-text-primary);
}
.drawing-popover button {
  height: 26px;
  border: 1px solid var(--ml-divider);
  border-radius: 5px;
  background: var(--ml-background);
  color: var(--ml-text-secondary);
  cursor: pointer;
  white-space: nowrap;
}
.drawing-popover button.active {
  border-color: var(--ml-accent);
  color: var(--ml-text-primary);
}
.drawing-popover button.danger {
  color: var(--ml-error);
}
.list-workbench {
  grid-template-columns: none;
}
.instrument-list {
  overflow: auto;
  border-right: 0;
}
.list-header-row,
.instrument-row {
  min-width: 0;
}
.column-header {
  position: relative;
  height: 28px;
  overflow: hidden;
  padding: 0;
  border-right: 1px solid var(--ml-divider);
  color: var(--ml-text-primary);
  font-family: SimHei, "黑体", "Microsoft YaHei", "PingFang SC", sans-serif;
  cursor: grab;
  font-size: 11px;
  line-height: 28px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.column-header.dragging {
  opacity: 0.45;
}
.column-resizer {
  position: absolute;
  z-index: 2;
  top: 0;
  right: -4px;
  width: 9px;
  height: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: col-resize;
}
.column-resizer::after {
  position: absolute;
  top: 5px;
  bottom: 5px;
  left: 4px;
  width: 1px;
  background: var(--ml-divider);
  content: "";
}
.column-resizer:hover::after,
.column-resizer:focus-visible::after {
  width: 2px;
  background: var(--ml-accent);
}
.workbench-resizer {
  position: relative;
  z-index: 6;
  width: 8px;
  height: 100%;
  padding: 0;
  border: 0;
  border-right: 1px solid var(--ml-divider);
  border-left: 1px solid var(--ml-divider);
  background: var(--ml-surface-elevated);
  cursor: col-resize;
}
.workbench-resizer span {
  position: absolute;
  top: 50%;
  left: 1px;
  width: 4px;
  height: 42px;
  transform: translateY(-50%);
  border-radius: 3px;
  background: var(--ml-divider);
}
.workbench-resizer:hover span,
.workbench-resizer:focus-visible span {
  background: var(--ml-accent);
}
:global(body.market-resizing) {
  cursor: col-resize;
  user-select: none;
}
.line-options {
  display: flex;
  align-items: center;
  gap: 3px;
  margin: 0;
  padding: 0;
  border: 0;
}
.line-options legend {
  float: left;
  margin-right: 2px;
  color: var(--ml-text-secondary);
  white-space: nowrap;
}
.line-options button {
  display: grid;
  place-items: center;
  width: 34px;
  padding: 0;
}
.line-width-preview,
.line-style-preview {
  display: block;
  width: 24px;
  min-height: 1px;
  background: currentColor;
}
.line-style-preview {
  height: 2px;
}
.line-style-dashed {
  background: repeating-linear-gradient(
    90deg,
    currentColor 0 7px,
    transparent 7px 11px
  );
}
.line-style-dotted {
  background: repeating-linear-gradient(
    90deg,
    currentColor 0 2px,
    transparent 2px 5px
  );
}
.line-style-dashdot {
  background: repeating-linear-gradient(
    90deg,
    currentColor 0 9px,
    transparent 9px 12px,
    currentColor 12px 14px,
    transparent 14px 18px
  );
}
.market-page {
  box-sizing: border-box;
  max-width: 100%;
  overflow-x: clip;
}
.target-section,
.all-section {
  min-width: 0;
}
.list-workbench {
  grid-template-columns: minmax(0, 50%) 1fr;
  min-width: 0;
}
.instrument-list {
  position: relative;
  min-width: 0;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  scrollbar-gutter: stable;
}
.list-header-row,
.instrument-row {
  display: grid;
  grid-template-columns: 34px 42px minmax(0, 1fr) 30px;
  min-width: 0;
}
.list-header-row {
  z-index: 8;
}
.flag-column-title,
.sequence-column-title,
.sequence-cell {
  display: grid;
  place-items: center;
  border-right: 1px solid var(--ml-divider);
  color: var(--ml-text-secondary);
  font-size: 11px;
}
.list-table-header,
.row-main {
  min-width: 0;
  overflow: hidden;
}
.instrument-row {
  position: relative;
  background: var(--ml-surface);
}
.instrument-row.flagged {
  background: color-mix(in srgb, var(--row-flag-color) 22%, var(--ml-surface));
}
.instrument-row.active {
  box-shadow: inset 3px 0 var(--ml-accent);
}
.instrument-row.active.flagged {
  background: color-mix(
    in srgb,
    var(--row-flag-color) 34%,
    var(--ml-surface-selected)
  );
}
.flag-cell {
  position: relative;
  display: grid;
  place-items: center;
  border-right: 1px solid var(--ml-divider);
}
.row-flag {
  display: grid;
  place-items: center;
  width: 26px;
  height: 24px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ml-text-disabled);
  cursor: pointer;
}
.row-flag svg {
  width: 15px;
  height: 15px;
  fill: transparent;
  stroke: currentColor;
  stroke-width: 1.8;
}
.row-flag.marked svg {
  fill: currentColor;
}
.flag-palette {
  position: absolute;
  z-index: 20;
  top: 24px;
  left: 3px;
  display: flex;
  gap: 4px;
  padding: 5px;
  border: 1px solid var(--ml-divider);
  border-radius: 6px;
  background: var(--ml-surface-elevated);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
}
.flag-palette button {
  width: 20px;
  height: 20px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--ml-text-primary) 28%, transparent);
  border-radius: 50%;
  cursor: pointer;
}
.flag-palette button.clear {
  display: grid;
  place-items: center;
  background: var(--ml-surface);
  color: var(--ml-text-secondary);
  font-weight: 700;
}
.list-table-header .column-resizer {
  cursor: col-resize;
}
.drawing-popover {
  overflow: visible;
}
.popover-drag-handle {
  display: grid !important;
  place-items: center;
  width: 27px !important;
  padding: 0 !important;
  border: 0 !important;
  background: transparent !important;
  cursor: move !important;
  touch-action: none;
}
.popover-drag-handle svg {
  width: 16px;
  height: 18px;
  fill: var(--ml-text-secondary);
}
.popover-color {
  width: 28px;
  height: 26px;
  padding: 1px;
  border: 1px solid var(--ml-divider);
  border-radius: 4px;
  background: transparent;
  cursor: pointer;
}
.popover-opacity {
  width: 70px;
}
.font-size-select {
  width: 70px;
}
.preview-select {
  position: relative;
}
.preview-select summary {
  display: grid;
  place-items: center;
  width: 40px;
  height: 26px;
  border: 1px solid var(--ml-divider);
  border-radius: 5px;
  background: var(--ml-background);
  color: var(--ml-text-primary);
  cursor: pointer;
  list-style: none;
}
.preview-select summary::-webkit-details-marker {
  display: none;
}
.preview-select > div {
  position: absolute;
  z-index: 20;
  top: 30px;
  left: 0;
  display: grid;
  gap: 3px;
  padding: 4px;
  border: 1px solid var(--ml-divider);
  border-radius: 5px;
  background: var(--ml-surface);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
}
.preview-select button {
  display: grid;
  place-items: center;
  width: 40px !important;
  padding: 0 !important;
}
.drawing-popover .icon-action {
  display: grid;
  place-items: center;
  width: 29px;
  padding: 0;
}
.drawing-popover .icon-action svg {
  width: 17px;
  height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
@media (max-width: 800px) {
  .market-page {
    width: calc(100vw - 24px);
    margin: 0 12px;
  }
  .list-workbench {
    grid-template-columns: 1fr !important;
    grid-template-rows: 240px 1fr;
    height: auto;
    min-height: 900px;
  }
  .workbench-resizer {
    display: none;
  }
  .instrument-list {
    border-right: 0;
    border-bottom: 1px solid var(--ml-divider);
  }
  .workbench-header span,
  .workbench-actions .el-button-group,
  .workbench-actions > .el-dropdown {
    display: none;
  }
  .drawing-popover {
    left: 8px;
    right: 8px;
    transform: none;
  }
}
.drawing-popover {
  gap: 5px;
  padding: 4px 6px;
  border-radius: 6px;
}
.drawing-popover button {
  height: 24px;
}
.drawing-popover .icon-action {
  width: 26px;
}
.drawing-popover .popover-drag-handle {
  width: 24px !important;
}
.all-section{margin-top:0}
.list-workbench{height:calc(100vh - 154px);min-height:520px}
.instrument-list{overflow:auto}
.list-header-row,.instrument-row{grid-template-columns:34px 42px minmax(max-content,1fr)}
.list-table-header,.row-main{min-width:max-content}
.instrument-row{height:33px;box-sizing:border-box}
.list-virtual-spacer{min-width:max-content;pointer-events:none}
.column-sort{display:grid;grid-template-columns:minmax(0,1fr) 1.2em;align-items:center;width:100%;height:28px;padding:0;border:0;background:transparent;color:inherit;font:inherit;cursor:pointer;text-align:left;white-space:nowrap;overflow:hidden}
.column-sort b{color:var(--ml-accent);text-align:center}
.column-sort span{overflow:hidden;text-overflow:ellipsis}
:global(.workbench-overlay){--detail-list-width:clamp(10rem,11vw,12rem);grid-template-columns:var(--detail-list-width) minmax(0,1fr) 48px;grid-template-rows:46px minmax(0,1fr) 36px;overflow:hidden}
.workbench-header{grid-column:1/-1;grid-template-rows:1fr;padding:0 10px}
.instrument-quote-line{grid-row:1;min-width:0}
.detail-instrument-list{grid-column:1;grid-row:2/4;min-height:0;overflow:auto;border-right:1px solid var(--ml-divider);background:var(--ml-surface)}
.detail-instrument-row{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-rows:auto auto;gap:3px 4px;width:100%;padding:7px 7px;border:0;border-bottom:1px solid var(--ml-divider);background:transparent;color:var(--ml-text-primary);cursor:pointer;text-align:left;font-variant-numeric:tabular-nums}
.detail-instrument-row:hover,.detail-instrument-row.active{background:var(--ml-surface-selected)}
.detail-instrument-row b,.detail-instrument-row small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.detail-instrument-row b{grid-column:1;grid-row:1;font-size:12px}.detail-instrument-row small{grid-column:1;grid-row:2;color:var(--ml-text-secondary);font-size:10px}.detail-instrument-row strong,.detail-instrument-row em{grid-column:2;text-align:right;font-style:normal;white-space:nowrap}.detail-instrument-row strong{grid-row:1;font-size:12px}.detail-instrument-row em{grid-row:2;font-size:10px}
.drawing-toolbar{grid-column:3;grid-row:2/4;position:relative;z-index:40;border-right:0;border-left:1px solid var(--ml-divider);overflow:visible}
.period-bar{grid-column:2;grid-row:3}.workbench-content{grid-column:2;grid-row:2;overflow:auto}.workbench-chart{min-height:0}
.workbench-content .replay-controls{position:sticky;bottom:0;left:auto;right:auto}
.indicator-delete{font-size:18px;line-height:1}
.indicator-manager {
  display: grid;
  gap: 9px;
  max-height: min(620px, 70vh);
  overflow: auto;
}
.indicator-catalog-source {
  margin: 0;
  color: var(--ml-text-secondary);
  font-size: 12px;
}
.indicator-add {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 7px;
}
.indicator-instance {
  padding: 8px;
  border: 1px solid var(--ml-divider);
  border-radius: 7px;
  background: var(--ml-background);
}
.indicator-instance > header {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 7px;
}
.indicator-instance header span {
  color: var(--ml-text-secondary);
  font-size: 11px;
}
.indicator-visibility,
.indicator-remove {
  border: 0;
  background: transparent;
  color: var(--ml-text-secondary);
  cursor: pointer;
}
.indicator-visibility {
  color: var(--ml-accent);
  font-size: 16px;
}
.indicator-remove {
  font-size: 19px;
}
.indicator-instance details {
  margin-top: 5px;
}
.indicator-instance summary {
  color: var(--ml-text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.indicator-settings {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 7px;
  margin-top: 8px;
}
.indicator-settings label {
  display: grid;
  grid-template-columns: 65px minmax(0, 1fr);
  align-items: center;
  gap: 5px;
  color: var(--ml-text-secondary);
  font-size: 11px;
}
.indicator-settings input[type="color"] {
  width: 42px;
  height: 26px;
  padding: 1px;
  border: 1px solid var(--ml-divider);
  background: transparent;
}
.indicator-error {
  margin: 5px 0 0;
  color: var(--ml-error);
  font-size: 11px;
}
.indicator-external-meta {
  margin: 5px 0 0;
  color: var(--ml-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
.indicator-add small {
  display: block;
  max-width: 220px;
  overflow: hidden;
  color: var(--ml-text-disabled);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:global(.indicator-manager-popper) {
  z-index: 3100 !important;
}

/* R4 续2：页面尺寸由父视口分配，图表不再依赖硬编码高度减值。 */
.market-page { width: 100%; height: 100%; min-height: 0; margin: 0; display: flex; flex-direction: column; overflow: hidden; }
.all-section { flex: 1 1 auto; min-height: 0; margin: 0; padding: 0; display: flex; flex-direction: column; }
.list-workbench { --market-toolbar-height: 42px; flex: 1 1 auto; height: auto; min-height: 0; border-radius: 0; grid-template-columns: minmax(360px, 1fr) 8px minmax(0, var(--chart-width, 52%)); }
.instrument-list { display: block; min-height: 0; overflow: auto; background: var(--ml-surface); }
.all-toolbar {
  --toolbar-select-width: 16em;
  --toolbar-input-width: clamp(10rem, 18vw, 22rem);
  --toolbar-button-width: clamp(3rem, 4vw, 4.5rem);
  position: sticky; top: 0; left: 0; z-index: 7; display: flex; flex-wrap: wrap;
  align-items: center; gap: 4px; padding: 5px 6px; box-sizing: border-box; font-size: 12px;
  background: var(--ml-surface); border-bottom: 1px solid var(--ml-divider);
}
.all-toolbar :deep(.el-select) { flex: 0 0 var(--toolbar-select-width); width: var(--toolbar-select-width); }
.all-toolbar :deep(.el-input) { flex: 0 0 var(--toolbar-input-width); width: var(--toolbar-input-width); }
.all-toolbar :deep(.el-button) { flex: 0 0 var(--toolbar-button-width); width: var(--toolbar-button-width); min-width: 0; margin: 0; padding-inline: 0; }
.list-header-row { top: var(--market-toolbar-height); z-index: 6; background: var(--ml-surface-elevated); box-shadow: 0 1px 0 var(--ml-divider); }
.list-table-header, .list-table-header > .column-header, .column-sort { background: var(--ml-surface-elevated); }
.list-table-header { gap: 0; }.column-header { margin: 0; border-right: 1px solid var(--ml-divider); }
.boards { gap: var(--ml-chart-board-gap, 12px); padding: 0; background: var(--ml-background); grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); }
.board { display: flex; min-height: 0; border: 1px solid var(--ml-divider); background: var(--ml-surface); }
.board > .kline-chart { flex: 1 1 auto; min-height: 0; }
.board-overlay, .detail-chart-overlay { display: flex; align-items: center; justify-content: space-between; gap: 5px; min-width: 0; font-size: 11px; color: var(--ml-text-primary); pointer-events: none; }
.instrument-info{display:grid;grid-template-rows:repeat(2,18px);align-items:center;font-size:11px;white-space:nowrap;color:var(--ml-text-primary)}
.instrument-info small{font-size:10px;color:var(--ml-text-secondary)}
.board-overlay select { box-sizing: border-box; width: 8.5ch; min-width: 8.5ch; height: 20px; padding: 0; color: var(--ml-text-primary); background: var(--ml-surface); border: 1px solid var(--ml-divider); border-radius: 3px; font-size: 10px; pointer-events: auto; }
.workbench-content { position: relative; overflow: hidden; }.workbench-chart { height: 100%; min-height: 0; }.workbench-chart > .kline-chart { height: 100%; }
.workbench-overlay { grid-template-rows: 42px minmax(0, 1fr) 36px; }.workbench-header { min-height: 42px; }.instrument-quote-line { display: none !important; }
.workbench-content{--replay-toolbar-height:44px}
.workbench-content.replay-open .workbench-chart{height:calc(100% - var(--replay-toolbar-height))}
.workbench-content .replay-controls { position: absolute; left: 0; right: 0; bottom: 0; z-index: 22; height:var(--replay-toolbar-height);box-sizing:border-box;justify-content:center;gap:6px;white-space:nowrap;overflow-x:auto;padding:0 6px }
.replay-controls button{flex:none;padding:5px 7px;background:transparent}.replay-controls button.active,.replay-controls button[aria-pressed=true]{background:var(--ml-surface-selected)}
.replay-controls select{color:var(--ml-text-primary);background:var(--ml-surface);border:0;padding:3px;font:inherit}
.row-main .quote-tone-up,.detail-instrument-row .quote-tone-up { color:var(--ml-price-up); }
.row-main .quote-tone-down,.detail-instrument-row .quote-tone-down { color:var(--ml-price-down); }
.row-main .quote-tone-flat,.detail-instrument-row .quote-tone-flat { color:var(--ml-text-primary); }
.row-main .quote-tone-missing,.detail-instrument-row .quote-tone-missing { color:var(--ml-text-disabled); }

/* R4 行情工作台：固定操作层级与日终快照状态，数值和来源口径不变。 */
.market-workspace-header { position: relative; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex: 0 0 auto; min-height: 46px; padding: 7px 12px; box-sizing: border-box; border: 1px solid var(--ml-divider); border-bottom: 0; background: var(--ml-surface); }
.workspace-heading, .workspace-actions { display: flex; align-items: center; gap: 10px; min-width: 0; }
.workspace-heading h1 { flex: 0 0 auto; margin: 0; color: var(--ml-text-primary); font-size: 15px; font-weight: 650; line-height: 24px; letter-spacing: -.01em; }
.workspace-category { min-width: 0; padding-left: 10px; border-left: 1px solid var(--ml-divider); color: var(--ml-text-secondary); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-count { flex: 0 0 auto; color: var(--ml-text-disabled); font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.workspace-count b { color: var(--ml-text-primary); font-weight: 500; }
.workspace-count-skeleton { width: 54px; height: 10px; border-radius: 3px; background: var(--ml-surface-selected); }
.workspace-actions { flex: 0 0 auto; gap: 7px; }
.workspace-status { display: inline-flex; align-items: center; gap: 6px; padding: 4px 7px; border: 1px solid var(--ml-divider); border-radius: 4px; background: var(--ml-surface-elevated); color: var(--ml-text-secondary); font-size: 10px; line-height: 16px; white-space: nowrap; }
.workspace-status i { width: 5px; height: 5px; border-radius: 50%; background: var(--ml-text-disabled); }
.workspace-status.pending i, .board-update-dot { background: var(--ml-accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ml-accent) 12%, transparent); }
.workspace-status.warning i { background: var(--ml-error); }
.workspace-status.pending { border-color: color-mix(in srgb, var(--ml-accent) 24%, var(--ml-divider)); }
.workspace-status.warning { border-color: color-mix(in srgb, var(--ml-error) 30%, var(--ml-divider)); color: var(--ml-error); }
.workspace-icon-button, .workspace-detail-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 29px; padding: 0 9px; border: 1px solid var(--ml-divider); border-radius: 5px; background: var(--ml-surface); color: var(--ml-text-secondary); font-size: 11px; cursor: pointer; transition: background 140ms, color 140ms, border-color 140ms; }
.workspace-icon-button { width: 29px; padding: 0; }
.workspace-detail-button { color: var(--ml-accent); border-color: color-mix(in srgb, var(--ml-accent) 35%, var(--ml-divider)); }
.workspace-icon-button svg, .workspace-detail-button svg, .board-actions svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
.workspace-icon-button:hover:not(:disabled), .workspace-detail-button:hover:not(:disabled) { border-color: color-mix(in srgb, var(--ml-accent) 45%, var(--ml-divider)); background: var(--ml-surface-selected); color: var(--ml-accent); }
.workspace-icon-button:disabled, .workspace-detail-button:disabled, .board-actions button:disabled { opacity: .4; cursor: default; }
.workspace-icon-button:active:not(:disabled), .workspace-detail-button:active:not(:disabled), .board-actions button:active:not(:disabled) { background: var(--ml-surface-selected); }
.workspace-progress { position: absolute; left: 0; right: 0; bottom: 0; height: 2px; overflow: hidden; background: color-mix(in srgb, var(--ml-accent) 9%, transparent); }
.workspace-progress i { display: block; height: 100%; background: var(--ml-accent); transition: width 180ms ease; }
.all-toolbar { display: grid; grid-template-columns: minmax(128px, .85fr) minmax(110px, 1fr) 50px; gap: 7px; width: 100%; min-height: 43px; padding: 6px 8px; }
.all-toolbar :deep(.el-select), .all-toolbar :deep(.el-input) { width: 100%; min-width: 0; }
.all-toolbar :deep(.el-button) { width: 50px; min-width: 0; height: 30px; font-size: 12px; border-radius: 4px; }
.all-toolbar :deep(.el-select__wrapper), .all-toolbar :deep(.el-input__wrapper) { min-height: 30px; height: 30px; box-sizing: border-box; font-size: 12px; border-radius: 4px; }
.all-toolbar :deep(.el-select__wrapper), .all-toolbar :deep(.el-input__wrapper) { background: var(--ml-surface-elevated); }
.market-search-icon { width: 14px; height: 14px; fill: none; stroke: var(--ml-text-secondary); stroke-width: 1.7; stroke-linecap: round; }
.market-sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.all-section > .page-alert { flex: 0 0 auto; padding: 6px 12px; border-radius: 0; border-inline: 1px solid var(--ml-divider); }
.all-section > .page-alert :deep(.el-alert__title) { font-size: 11px; line-height: 17px; }
.list-header-row { height: 30px; box-sizing: border-box; }
.column-sort { height: 29px; padding: 0 7px; font-size: 11px; font-weight: 500; }
.column-header.is-numeric .column-sort { text-align: right; }
.column-header.is-sorted .column-sort { color: var(--ml-accent); font-weight: 650; background: color-mix(in srgb, var(--ml-accent) 7%, var(--ml-surface-elevated)); }
.column-sort:hover { color: var(--ml-accent); }
.instrument-row { border-bottom-color: color-mix(in srgb, var(--ml-divider) 70%, transparent); transition: background-color 120ms ease; }
.instrument-row:hover:not(.active) { background: color-mix(in srgb, var(--ml-surface-selected) 70%, var(--ml-surface)); }
.instrument-row.active { background: var(--ml-surface-selected); box-shadow: inset 3px 0 var(--ml-accent); }
.instrument-row.active .sequence-cell { color: var(--ml-accent); font-weight: 600; }
.instrument-row.active .column-name { font-weight: 650; }
.instrument-row:focus-within { box-shadow: inset 3px 0 var(--ml-accent), inset 0 0 0 1px color-mix(in srgb, var(--ml-accent) 45%, transparent); }
.row-main > span { padding: 5px 8px; border-right-color: color-mix(in srgb, var(--ml-divider) 35%, transparent); line-height: 22px; font-variant-numeric: tabular-nums; text-align: right; }
.row-main > .column-name, .row-main > .column-symbol { text-align: left; }
.row-main > .column-name { font-weight: 500; }
.row-main > .column-latestPrice { font-weight: 600; }
.row-main > .column-symbol { font-size: 11px; font-weight: 500; color: var(--ml-text-secondary); }
.row-main:focus-visible { outline: 0; }
.row-flag { border-radius: 4px; opacity: .65; transition: opacity 120ms, background 120ms; }
.instrument-row:hover .row-flag, .row-flag.marked, .row-flag:focus-visible { opacity: 1; }
.row-flag:hover { background: color-mix(in srgb, var(--ml-accent) 10%, transparent); }
.flag-palette { gap: 6px; padding: 7px; border-radius: 7px; box-shadow: 0 6px 18px color-mix(in srgb, var(--ml-text-primary) 13%, transparent); }
.flag-palette button { position: relative; width: 21px; height: 21px; transition: box-shadow 120ms ease; }
.flag-palette button[aria-checked='true'] { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
.flag-palette button:focus-visible { box-shadow: 0 0 0 3px var(--ml-surface), 0 0 0 5px var(--ml-accent); outline: 0; }
.workbench-resizer { background: var(--ml-background); border-inline: 1px solid var(--ml-divider); }
.workbench-resizer:hover { background: color-mix(in srgb, var(--ml-accent) 10%, var(--ml-background)); }
.market-list-empty { display: flex; flex-direction: column; align-items: center; gap: 12px; min-width: 200px; padding: 55px 16px; color: var(--ml-text-secondary); text-align: center; }
.market-list-empty svg { width: 32px; height: 32px; fill: none; stroke: var(--ml-text-disabled); stroke-width: 1.5; stroke-linecap: round; }
.market-list-empty strong { color: var(--ml-text-primary); font-size: 13px; font-weight: 550; }
.market-list-empty p { max-width: 30em; margin: -5px 0 0; color: var(--ml-text-secondary); font-size: 11px; line-height: 1.7; }
.market-empty-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
.market-list-empty button { padding: 5px 12px; border: 1px solid var(--ml-divider); border-radius: 4px; background: var(--ml-surface); color: var(--ml-accent); font-size: 12px; cursor: pointer; }
.market-list-empty button:hover { border-color: var(--ml-accent); background: var(--ml-surface-selected); }
.boards { gap: 8px; }
.board { flex-direction: column; }
.board:focus-within { border-color: color-mix(in srgb, var(--ml-accent) 45%, var(--ml-divider)); }
.board-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex: 0 0 auto; min-height: 32px; padding: 3px 8px; border-bottom: 1px solid var(--ml-divider); box-sizing: border-box; background: var(--ml-surface); }
.board-instrument { display: flex; align-items: center; gap: 8px; min-width: 0; }
.board-instrument strong { flex: 0 0 auto; color: var(--ml-text-primary); font: 600 12px ui-monospace, Consolas, monospace; }
.board-instrument span { min-width: 0; overflow: hidden; color: var(--ml-text-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.board:hover .board-instrument span, .board:focus-within .board-instrument span { color: var(--ml-text-primary); }
.board-actions { display: flex; align-items: center; gap: 7px; flex: 0 0 auto; }
.board-zoom { display: flex; align-items: center; gap: 1px; }
.board-actions select { width: 68px; height: 23px; padding: 0 3px; border: 1px solid var(--ml-divider); border-radius: 4px; background: var(--ml-surface); color: var(--ml-text-primary); font-size: 11px; font-family: inherit; cursor: pointer; }
.board-actions button { display: grid; place-items: center; width: 23px; height: 23px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--ml-text-secondary); cursor: pointer; }
.board-actions button:hover:not(:disabled) { color: var(--ml-accent); background: var(--ml-surface-selected); }
.board-actions button { opacity: .65; transition: opacity 120ms ease, color 120ms ease, background 120ms ease; }
.board:hover .board-actions button, .board-actions button:focus-visible { opacity: 1; }
.board-status { color: var(--ml-text-secondary); font-size: 10px; }
.board-status.warning { color: var(--ml-error); }
.board-status.pending { color: var(--ml-accent); }
.board-update-dot { width: 5px; height: 5px; border-radius: 50%; }
.board > .market-skeleton, .board > .kline-chart { flex: 1 1 auto; min-height: 0; height: 0; }
.workspace-icon-button:focus-visible, .workspace-detail-button:focus-visible, .column-sort:focus-visible, .row-flag:focus-visible, .board-actions select:focus-visible, .board-actions button:focus-visible, .market-list-empty button:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: -2px; }
@media (max-width: 920px) {
  .market-page { overflow-y: auto; }
  .market-workspace-header { gap: 8px; flex-wrap: wrap; }
  .workspace-heading { gap: 7px; }
  .workspace-category { max-width: 38vw; }
  .workspace-status { display: none; }
  .list-workbench { flex: 0 0 auto; grid-template-columns: 1fr !important; grid-template-rows: minmax(240px, 34vh) minmax(560px, 64vh); min-height: 800px; }
  .workbench-resizer { display: none; }
  .instrument-list { border-bottom: 1px solid var(--ml-divider); }
  .boards { gap: 8px; }
}
@media (max-width: 480px) {
  .market-workspace-header { padding-inline: 8px; }
  .workspace-heading h1 { font-size: 14px; }
  .workspace-category { max-width: 42vw; font-size: 11px; }
  .workspace-count { display: none; }
  .workspace-detail-button { font-size: 10px; }
  .all-toolbar { grid-template-columns: minmax(104px, .8fr) minmax(92px, 1fr) 44px; gap: 5px; padding-inline: 6px; }
  .all-toolbar :deep(.el-button) { width: 44px; }
}
@media (prefers-reduced-motion: reduce) {
  .workspace-progress i, .workspace-icon-button, .workspace-detail-button, .instrument-row, .row-flag, .flag-palette button, .board-actions button { transition: none; }
}
</style>
