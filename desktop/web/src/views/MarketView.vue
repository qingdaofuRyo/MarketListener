<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import KLineChart, { type ChartDrawing, type ChartDrawingStyle, type DrawingLineStyle, type DrawingTool, type KLineBar } from "../components/charts/KLineChart.vue";
import MiniKLine from "../components/charts/MiniKLine.vue";
import DrawingColorPicker from "../components/charts/DrawingColorPicker.vue";
import { apiGet, apiPost, apiPut, formatAssetType, formatMarket, formatNumber, invalidateQuery } from "../domain/api";

interface Instrument {
  instrumentId: string; symbol?: string; name?: string; market?: string; assetType?: string; seriesKind?: string; period?: string;
  latestPrice?: number; lastClose?: number; totalMarketCap?: number; floatMarketCap?: number; openInterest?: number; capitalDeposit?: number; capitalDepositReason?: string;
  matchedStrategyIds?: string[]; nightSession?: string; actualSource?: string; source?: string;
}
interface Strategy { strategyId: string; displayName: string; }
interface MarketCategory { id: string; label: string; }
interface BarsMeta { total: number; period: string; availablePeriods: string[]; earliestBarAt?: string; latestBarAt?: string; dataVersion?: string; }
interface History extends BarsMeta { start: number; size: number; bars: KLineBar[]; before?: string; hasMore?: boolean; }
interface ChartBootstrap extends History { series: Record<string, Array<number | null>>; drawings: ChartDrawing[]; }
interface Board { period: string; bars: KLineBar[]; loading: boolean; loadingEarlier: boolean; start: number; total: number; before?: string; hasMore: boolean; availablePeriods: string[]; requestId: number; instrumentId: string; abort?: AbortController; }
type ListColumnKey = "symbol" | "name" | "close" | "market" | "source";
interface ListColumn { id: ListColumnKey; label: string; }
interface FlagChoice { color: string; label: string; }
type DrawingKind = ChartDrawing["type"];
interface DrawingPreferences {
  magnet: boolean;
  crossPeriod: boolean;
  keepDrawing: boolean;
  styles: Partial<Record<DrawingKind, ChartDrawingStyle>>;
}

const fallbackCategories: MarketCategory[] = [
  ["all", "全部市场"], ["a-index", "A股-指数"], ["tdx-industry-index", "通达信-行业板块指数"], ["tdx-board-index", "通达信-综合板块指数"], ["a-sh", "A股-沪市"], ["a-sz", "A股-深市"], ["a-bse", "A股-北证"],
  ["a-chinext", "A股-创业板"], ["a-star", "A股-科创板"], ["a-etf", "A股-ETF基金"], ["a-convertible", "A股-可转债"], ["a-exchangeable", "A股-可交债"],
  ["a-pledged-repo", "A股-债券通用质押式回购"], ["a-repo", "A股-债券回购"], ["a-lof", "A股-LOF基金"], ["a-reit", "A股-REITs"], ["hk-index", "港股-指数"], ["hk-stock", "港股-个股"],
  ["cn-future-index", "国内期货-指数"], ["cn-future-cffex", "国内期货-中金所"], ["cn-future-commodity", "国内期货-商品期货"], ["cn-future-night", "国内期货-商品期货夜盘"], ["other", "其它"],
].map(([id, label]) => ({ id, label }));
const periodOptions = [
  ["5m", "5分"], ["15m", "15分"], ["30m", "30分"], ["1h", "60分"], ["2h", "120分"],
  ["1d", "日线"], ["1w", "周线"], ["1mo", "月线"], ["3mo", "季线"], ["1y", "年线"],
] as const;
const quoteFields: Array<[ListColumnKey, string]> = [["symbol", "代码"], ["name", "名称"], ["close", "收盘价"], ["market", "市场类型"], ["source", "数据源"]];
const defaultListColumns: ListColumn[] = quoteFields.map(([id, label]) => ({ id, label }));
const drawingTools: Array<{ id: DrawingTool; label: string; path: string }> = [
  { id: "cursor", label: "光标", path: "M5 3l13 8-6 2-3 6z" },
  { id: "horizontal", label: "水平线", path: "M3 12h6M15 12h6M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0" },
  { id: "vertical", label: "垂直线", path: "M12 3v6M12 15v6M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0" },
  { id: "rectangle", label: "箱体线", path: "M4 5h16v14H4zM2 12a2 2 0 1 0 4 0a2 2 0 1 0-4 0M18 12a2 2 0 1 0 4 0a2 2 0 1 0-4 0" },
  { id: "text", label: "文本框", path: "M5 5h14M12 5v14M8 19h8" },
];
const drawingColorPresets = [
  "#ff1744", "#ff5722", "#ff9800", "#ffc107", "#cddc39", "#4caf50", "#00bcd4", "#2196f3",
  "#3f51b5", "#9c27b0", "#e91e63", "#f44336", "#ff4081", "#ff6d00", "#ffee58", "#76ff03",
  "#00e5ff", "#448aff", "#651fff", "#d500f9", "#ff3d00", "#00e676", "#18ffff", "#c6ff00",
];
const lineStyles: Array<[DrawingLineStyle, string]> = [["solid", "实线"], ["dashed", "虚线"], ["dotted", "点线"], ["dashdot", "一长一短"]];
const drawingPreferenceKey = "market-drawing-preferences-v1";
const flagChoices: FlagChoice[] = [
  { color: "", label: "清除标记" }, { color: "#ef4444", label: "红色" }, { color: "#f97316", label: "橙色" },
  { color: "#eab308", label: "黄色" }, { color: "#22c55e", label: "绿色" }, { color: "#3b82f6", label: "蓝色" }, { color: "#a855f7", label: "紫色" },
];

function baseDrawingStyle(type: DrawingKind): ChartDrawingStyle {
  return { color: "#2196f3", width: 1.5, lineStyle: "solid", fillColor: "rgba(33,150,243,0.100)", fillOpacity: 1, fontSize: 14, borderColor: "transparent", borderWidth: 0, borderStyle: "solid", locked: false };
}
function storedDrawingPreferences(): DrawingPreferences {
  const fallback: DrawingPreferences = { magnet: false, crossPeriod: true, keepDrawing: false, styles: {} };
  try {
    const value = JSON.parse(localStorage.getItem(drawingPreferenceKey) || "{}") as Partial<DrawingPreferences>;
    return {
      magnet: typeof value.magnet === "boolean" ? value.magnet : fallback.magnet,
      crossPeriod: typeof value.crossPeriod === "boolean" ? value.crossPeriod : fallback.crossPeriod,
      keepDrawing: typeof value.keepDrawing === "boolean" ? value.keepDrawing : fallback.keepDrawing,
      styles: value.styles && typeof value.styles === "object" ? value.styles : {},
    };
  } catch { return fallback; }
}
const storedDrawingOptions = storedDrawingPreferences();
const drawingStyleDefaults = ref<Record<DrawingKind, ChartDrawingStyle>>({
  horizontal: { ...baseDrawingStyle("horizontal"), ...storedDrawingOptions.styles.horizontal },
  vertical: { ...baseDrawingStyle("vertical"), ...storedDrawingOptions.styles.vertical },
  rectangle: { ...baseDrawingStyle("rectangle"), ...storedDrawingOptions.styles.rectangle },
  text: { ...baseDrawingStyle("text"), ...storedDrawingOptions.styles.text },
});

const categories = ref<MarketCategory[]>(fallbackCategories);
const strategies = ref<Strategy[]>([]);
const allItems = ref<Instrument[]>([]); const allTotal = ref(0); const category = ref("all"); const query = ref(""); const page = ref(1); const cardPageSize = ref(10);
const storedView = localStorage.getItem("market-all-view");
const view = ref<"card" | "list">(storedView === "card" || storedView === "list" ? storedView : "list"); const loading = ref(false); const error = ref("");
const targetItems = ref<Instrument[]>([]); const targetLoading = ref(false); const selectedTargetMarkets = ref<string[]>([]); const selectedTargetStrategies = ref<string[]>([]);
const selected = ref<Instrument>(); const fullscreen = ref(false); const detailLoading = ref(false); const selectedDrawingId = ref("");
const marketVersion = ref("");
const history = ref<History>({ total: 0, period: "1d", availablePeriods: [], start: 0, size: 0, bars: [], hasMore: false });
const indicators = ref<string[]>(["volume"]); const indicatorValues = ref<Record<string, Array<number | null>>>({});
const inverse = ref(localStorage.getItem("market-chart-inverse") === "true"); const swapColors = ref(localStorage.getItem("market-chart-swap") === "true");
const drawings = ref<ChartDrawing[]>([]); const drawingsInstrumentId = ref(""); const tool = ref<DrawingTool>("cursor"); const magnet = ref(storedDrawingOptions.magnet); const crossPeriod = ref(storedDrawingOptions.crossPeriod); const hiddenDrawings = ref(false); const keepDrawing = ref(storedDrawingOptions.keepDrawing);
const cardDrawings = ref<Record<string, ChartDrawing[]>>({});
const emptyBoard = (period: string): Board => ({ period, bars: [], loading: false, loadingEarlier: false, start: 0, total: 0, hasMore: false, availablePeriods: [], requestId: 0, instrumentId: "" });
const boardTop = ref<Board>(emptyBoard(storedPeriod("market-board-top", "1d"))); const boardBottom = ref<Board>(emptyBoard(storedPeriod("market-board-bottom", "1h")));
const listColumns = ref<ListColumn[]>(storedListColumns()); const draggingColumn = ref<ListColumnKey>();
const columnWidths = ref<Record<ListColumnKey, number>>(storedColumnWidths());
const rowFlags = ref<Record<string, string>>(storedRowFlags());
const flagPickerInstrumentId = ref("");
const chartWidthVw = ref(storedChartWidth());
const resizingColumn = ref<{ id: ListColumnKey; startX: number; startWidth: number }>();
const resizingSplit = ref(false);
const detailEarlierLoading = ref(false);
const drawingPopoverPosition = ref<{ left: number; top: number }>();
const drawingPopoverDrag = ref<{ pointerId: number; startX: number; startY: number; originLeft: number; originTop: number }>();
const workbenchChart = ref<HTMLElement>();
const drawingPopoverElement = ref<HTMLElement>();
const viewportHeight = ref(720);
let searchTimer: ReturnType<typeof setTimeout> | undefined; let serial = 0; let allSerial = 0; let cardDrawingsSerial = 0; let historyAbort: AbortController | undefined; let allAbort: AbortController | undefined;
let drawingSaveChain: Promise<unknown> = Promise.resolve();

const visibleDrawings = computed(() => drawings.value.filter((item) => item.crossPeriod || item.period === history.value.period).map((item) => ({ ...item, hidden: hiddenDrawings.value || item.hidden })));
const selectedDrawing = computed(() => drawings.value.find((item) => item.id === selectedDrawingId.value));
const boardChartHeight = computed(() => Math.max(230, Math.floor((viewportHeight.value - 52 - 66 - 84) / 2)));
const detailChartHeight = computed(() => Math.max(480, viewportHeight.value - 98));
const hasMore = computed(() => allItems.value.length < allTotal.value);
const pageSize = computed(() => view.value === "list" ? 20 : cardPageSize.value);
const listGridTemplate = computed(() => listColumns.value.map((item) => `${columnWidths.value[item.id]}px`).join(" "));
const workbenchGridTemplate = computed(() => `minmax(0, 1fr) 8px minmax(0, ${chartWidthVw.value}vw)`);
const drawingPopoverStyle = computed(() => drawingPopoverPosition.value ? { left: `${drawingPopoverPosition.value.left}px`, top: `${drawingPopoverPosition.value.top}px`, transform: "none" } : undefined);
function noData(value: unknown, digits = 2): string { return typeof value === "number" && Number.isFinite(value) ? formatNumber(value, digits) : "—"; }
function marketText(item: Partial<Instrument>): string { return `${formatMarket(item.market)} · ${formatAssetType(item.assetType)}`; }
function storedPeriod(key: string, fallback: string): string { const value = localStorage.getItem(key) || fallback; return periodOptions.some(([period]) => period === value) ? value : fallback; }
function storedListColumns(): ListColumn[] { try { const ids = JSON.parse(localStorage.getItem("market-list-columns") || "[]") as ListColumnKey[]; return ids.length === defaultListColumns.length && defaultListColumns.every((item) => ids.includes(item.id)) ? ids.map((id) => defaultListColumns.find((item) => item.id === id)!) : [...defaultListColumns]; } catch { return [...defaultListColumns]; } }
function storedColumnWidths(): Record<ListColumnKey, number> {
  const defaults: Record<ListColumnKey, number> = { symbol: 96, name: 132, close: 92, market: 152, source: 126 };
  try {
    const stored = JSON.parse(localStorage.getItem("market-list-column-widths") || "{}") as Partial<Record<ListColumnKey, number>>;
    for (const key of Object.keys(defaults) as ListColumnKey[]) if (typeof stored[key] === "number") defaults[key] = Math.max(64, Math.min(360, stored[key]!));
  } catch { /* 使用默认列宽 */ }
  return defaults;
}
function storedRowFlags(): Record<string, string> {
  try {
    const stored = JSON.parse(localStorage.getItem("market-list-row-flags") || "{}") as Record<string, unknown>;
    return Object.fromEntries(Object.entries(stored).filter((entry): entry is [string, string] => typeof entry[1] === "string" && flagChoices.some((choice) => choice.color === entry[1])));
  } catch { return {}; }
}
function storedChartWidth(): number { const width = Number(localStorage.getItem("market-list-chart-width-vw")); return Number.isFinite(width) ? Math.max(25, Math.min(75, width)) : 50; }
function quoteValue(item: Instrument, field: ListColumnKey): string { if (field === "symbol") return item.symbol || item.instrumentId; if (field === "name") return item.name || "—"; if (field === "market") return marketText(item); if (field === "source") return item.actualSource || item.source || "本地"; return noData(item.latestPrice ?? item.lastClose, 4); }
function reorderColumn(target: ListColumnKey): void { const source = draggingColumn.value; if (!source || source === target) return; const next = [...listColumns.value]; const from = next.findIndex((item) => item.id === source); const to = next.findIndex((item) => item.id === target); const [moved] = next.splice(from, 1); next.splice(to, 0, moved); listColumns.value = next; localStorage.setItem("market-list-columns", JSON.stringify(next.map((item) => item.id))); draggingColumn.value = undefined; }
function updateViewport(): void { viewportHeight.value = window.innerHeight; }
function persistView(next: "card" | "list"): void {
  if (view.value === next) return;
  allAbort?.abort(); ++allSerial;
  if (next === "card") {
    boardTop.value.abort?.abort(); boardBottom.value.abort?.abort();
    ++boardTop.value.requestId; ++boardBottom.value.requestId; boardTop.value.loading = false; boardBottom.value.loading = false;
  }
  const previousInstrumentId = selected.value?.instrumentId;
  view.value = next; localStorage.setItem("market-all-view", next); page.value = 1; allItems.value = [];
  void loadAll().then(() => { if (next === "list" && selected.value?.instrumentId === previousInstrumentId) void loadBoards(); });
}
function startColumnResize(event: PointerEvent, id: ListColumnKey): void { event.preventDefault(); event.stopPropagation(); resizingColumn.value = { id, startX: event.clientX, startWidth: columnWidths.value[id] }; document.body.classList.add("market-resizing"); }
function resizeByKeyboard(id: ListColumnKey, direction: number): void { columnWidths.value = { ...columnWidths.value, [id]: Math.max(64, Math.min(360, columnWidths.value[id] + direction * 8)) }; persistColumnWidths(); }
function persistColumnWidths(): void { localStorage.setItem("market-list-column-widths", JSON.stringify(columnWidths.value)); }
function startSplitResize(event: PointerEvent): void { event.preventDefault(); resizingSplit.value = true; document.body.classList.add("market-resizing"); }
function resizePointer(event: PointerEvent): void {
  if (resizingColumn.value) {
    const { id, startX, startWidth } = resizingColumn.value;
    columnWidths.value = { ...columnWidths.value, [id]: Math.max(64, Math.min(360, startWidth + event.clientX - startX)) };
  }
  if (resizingSplit.value) {
    const pageRight = document.documentElement.clientWidth * .98;
    chartWidthVw.value = Math.max(25, Math.min(75, ((pageRight - event.clientX) / document.documentElement.clientWidth) * 100));
    window.dispatchEvent(new Event("resize"));
  }
}
function stopResize(): void {
  if (resizingColumn.value) persistColumnWidths();
  if (resizingSplit.value) localStorage.setItem("market-list-chart-width-vw", chartWidthVw.value.toFixed(2));
  resizingColumn.value = undefined; resizingSplit.value = false; document.body.classList.remove("market-resizing");
}
function resizeSplitByKeyboard(direction: number): void { chartWidthVw.value = Math.max(25, Math.min(75, chartWidthVw.value + direction)); localStorage.setItem("market-list-chart-width-vw", chartWidthVw.value.toFixed(2)); window.dispatchEvent(new Event("resize")); }
function startDrawingPopoverDrag(event: PointerEvent): void {
  const handle = event.currentTarget as HTMLElement;
  const popover = handle.closest<HTMLElement>(".drawing-popover"); const container = popover?.parentElement;
  if (!popover || !container) return;
  event.preventDefault(); handle.setPointerCapture(event.pointerId);
  const rect = popover.getBoundingClientRect(); const parentRect = container.getBoundingClientRect();
  const originLeft = rect.left - parentRect.left; const originTop = rect.top - parentRect.top;
  drawingPopoverPosition.value = { left: originLeft, top: originTop };
  drawingPopoverDrag.value = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, originLeft, originTop };
}
function moveDrawingPopover(event: PointerEvent): void {
  const drag = drawingPopoverDrag.value; if (!drag || drag.pointerId !== event.pointerId) return;
  const handle = event.currentTarget as HTMLElement; const popover = handle.closest<HTMLElement>(".drawing-popover"); const container = popover?.parentElement;
  if (!popover || !container) return;
  const left = Math.max(0, Math.min(container.clientWidth - popover.offsetWidth, drag.originLeft + event.clientX - drag.startX));
  const top = Math.max(0, Math.min(container.clientHeight - popover.offsetHeight, drag.originTop + event.clientY - drag.startY));
  drawingPopoverPosition.value = { left, top };
}
function stopDrawingPopoverDrag(event: PointerEvent): void {
  if (drawingPopoverDrag.value?.pointerId !== event.pointerId) return;
  const handle = event.currentTarget as HTMLElement;
  if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
  drawingPopoverDrag.value = undefined;
}
function closePreviewSelect(event: Event): void { (event.currentTarget as HTMLElement).closest("details")?.removeAttribute("open"); }
function onSelectDrawing(id: string, anchor?: { left: number; top: number }): void {
  selectedDrawingId.value = id;
  if (!id) { drawingPopoverPosition.value = undefined; return; }
  if (!anchor) return;
  void nextTick(() => {
    const popover = drawingPopoverElement.value;
    const container = workbenchChart.value;
    if (!popover || !container) return;
    const width = popover.offsetWidth || 320;
    const left = Math.max(0, Math.min(anchor.left - width / 2, container.clientWidth - width));
    const below = anchor.top + 12;
    const above = anchor.top - popover.offsetHeight - 12;
    const preferredTop = below + popover.offsetHeight <= container.clientHeight || above < 0 ? below : above;
    const top = Math.max(0, Math.min(preferredTop, container.clientHeight - popover.offsetHeight));
    drawingPopoverPosition.value = { left, top };
  });
}
function changeCardPageSize(size: number): void { cardPageSize.value = size; page.value = 1; allItems.value = []; void loadAll(); }
function selectDrawingTool(next: DrawingTool): void { tool.value = next; selectedDrawingId.value = ""; drawingPopoverPosition.value = undefined; }
function dismissDrawingPopover(event: PointerEvent): void {
  const target = event.target as HTMLElement | null;
  if (target?.closest(".drawing-popover") || target?.closest(".workbench-chart")) return;
  onSelectDrawing("");
}

async function loadMarketVersion(): Promise<void> {
  try { marketVersion.value = (await apiGet<{ dataVersion: string }>("/api/market/cache-status", undefined, { ttlMs: 0, persist: false, force: true })).dataVersion; } catch { marketVersion.value = ""; }
}
async function loadCategories(): Promise<void> { try { categories.value = (await apiGet<{ items: MarketCategory[] }>("/api/market/categories", undefined, { ttlMs: 60 * 60_000, persist: true })).items; } catch { categories.value = fallbackCategories; } }
async function loadStrategies(): Promise<void> { try { strategies.value = (await apiGet<{ items: Strategy[] }>("/api/strategy/definitions", undefined, { ttlMs: 5 * 60_000, persist: true })).items; } catch { strategies.value = []; } }
async function loadAll(append = false, targetPage = 1): Promise<void> {
  if (append && loading.value) return;
  if (!append) allAbort?.abort();
  const controller = append ? allAbort : new AbortController(); if (!append) allAbort = controller;
  const request = ++allSerial; const requestedView = view.value;
  loading.value = true; error.value = "";
  try {
    const requestedPage = append ? page.value + 1 : targetPage;
    const data = await apiGet<{ items: Instrument[]; total: number; dataVersion?: string }>("/api/market/instruments", { categoryKey: category.value === "all" ? undefined : category.value, q: query.value, page: requestedPage, pageSize: pageSize.value, version: marketVersion.value || undefined }, { ttlMs: 5 * 60_000, persist: true, signal: controller?.signal });
    if (request !== allSerial || requestedView !== view.value) return;
    page.value = requestedPage; allItems.value = append ? [...allItems.value, ...data.items] : data.items; allTotal.value = data.total;
    if (data.dataVersion) marketVersion.value = data.dataVersion;
    if ((!selected.value || !allItems.value.some((item) => item.instrumentId === selected.value?.instrumentId)) && allItems.value.length) selected.value = allItems.value[0];
    if (requestedView === "card") await loadCardDrawings(allItems.value);
  } catch (reason) { if (request === allSerial && !(reason instanceof DOMException && reason.name === "AbortError")) error.value = reason instanceof Error ? reason.message : "行情加载失败"; } finally { if (request === allSerial) loading.value = false; }
}
async function loadTargets(): Promise<void> {
  if (!strategies.value.length) { targetItems.value = []; return; }
  targetLoading.value = true;
  try {
    targetItems.value = (await apiPost<{ items: Instrument[] }>("/api/strategy/matches", {
      strategyIds: selectedTargetStrategies.value, allStrategies: selectedTargetStrategies.value.length === 0,
      categoryKeys: selectedTargetMarkets.value, page: 1, pageSize: 100,
    })).items;
  } catch { targetItems.value = []; } finally { targetLoading.value = false; }
}
function resetAll(): void { page.value = 1; allItems.value = []; void loadAll(); }
function search(): void { if (searchTimer) clearTimeout(searchTimer); searchTimer = setTimeout(resetAll, 250); }
function searchNow(): void { if (searchTimer) clearTimeout(searchTimer); resetAll(); }
function toggleFilter(selectedValues: string[], id: string): string[] { return selectedValues.includes(id) ? selectedValues.filter((item) => item !== id) : [...selectedValues, id]; }
function toggleTargetMarket(id: string): void { selectedTargetMarkets.value = id === "all" ? [] : toggleFilter(selectedTargetMarkets.value, id); void loadTargets(); }
function toggleTargetStrategy(id: string): void { selectedTargetStrategies.value = id === "all" ? [] : toggleFilter(selectedTargetStrategies.value, id); void loadTargets(); }
function choose(item: Instrument): void { selected.value = item; }
function listScroll(event: Event): void { const element = event.currentTarget as HTMLElement; if (hasMore.value && element.scrollTop + element.clientHeight >= element.scrollHeight - 160) void loadAll(true); }
function listWheel(event: WheelEvent): void {
  const element = event.currentTarget as HTMLElement;
  const multiplier = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 24 : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? element.clientHeight : 1;
  element.scrollTop += event.deltaY * multiplier;
  listScroll(event);
}
async function loadCardDrawings(items: Instrument[]): Promise<void> {
  const instrumentIds = [...new Set(items.map((item) => item.instrumentId).filter(Boolean))];
  const request = ++cardDrawingsSerial;
  if (!instrumentIds.length) { cardDrawings.value = {}; return; }
  await drawingSaveChain.catch(() => undefined);
  try {
    const data = await apiGet<{ items: Record<string, ChartDrawing[]> }>("/api/market/drawings/batch", { instrumentIds: instrumentIds.join(","), period: "1d" }, { ttlMs: 0, persist: false, force: true });
    if (request === cardDrawingsSerial && view.value === "card") cardDrawings.value = data.items ?? {};
  } catch { if (request === cardDrawingsSerial) cardDrawings.value = {}; }
}
function toggleFlagPicker(instrumentId: string): void { flagPickerInstrumentId.value = flagPickerInstrumentId.value === instrumentId ? "" : instrumentId; }
function setRowFlag(instrumentId: string, color: string): void {
  const next = { ...rowFlags.value };
  if (color) next[instrumentId] = color; else delete next[instrumentId];
  rowFlags.value = next; flagPickerInstrumentId.value = "";
  localStorage.setItem("market-list-row-flags", JSON.stringify(next));
}
function rowFlagStyle(item: Instrument): Record<string, string> { const color = rowFlags.value[item.instrumentId]; return color ? { "--row-flag-color": color } : {}; }

async function loadBoard(instrumentId: string, board: Board): Promise<void> {
  board.abort?.abort(); const controller = new AbortController(); board.abort = controller; const requestId = ++board.requestId;
  board.instrumentId = instrumentId; board.loading = true; board.loadingEarlier = false; board.bars = []; board.start = 0; board.total = 0; board.before = undefined; board.hasMore = false; board.availablePeriods = [];
  try {
    const data = await apiGet<{ bars: KLineBar[]; start?: number; size?: number; total: number; historyTotal?: number; before?: string; hasMore?: boolean; availablePeriods: string[] }>(`/api/market/instruments/${encodeURIComponent(instrumentId)}/bars`, { period: board.period, limit: 60, version: marketVersion.value || undefined }, { ttlMs: 5 * 60_000, persist: true, signal: controller.signal });
    if (requestId !== board.requestId || board.instrumentId !== instrumentId) return;
    board.bars = data.bars; board.start = data.start ?? Math.max(0, (data.historyTotal ?? data.total) - data.bars.length); board.total = data.historyTotal ?? data.total; board.before = data.before ?? data.bars[0]?.barOpenTime ?? data.bars[0]?.tradingDate; board.hasMore = data.hasMore ?? board.start > 0; board.availablePeriods = data.availablePeriods;
  } catch (reason) {
    if (requestId === board.requestId && !(reason instanceof DOMException && reason.name === "AbortError")) board.bars = [];
  } finally { if (requestId === board.requestId) board.loading = false; }
}
async function loadBoards(): Promise<void> {
  if (!selected.value) return; const instrumentId = selected.value.instrumentId;
  await Promise.all([loadBoard(instrumentId, boardTop.value), loadBoard(instrumentId, boardBottom.value), loadDrawings(instrumentId)]);
}
function boardPeriod(which: "top" | "bottom", period: string): void { const board = which === "top" ? boardTop.value : boardBottom.value; board.period = period; localStorage.setItem(`market-board-${which}`, period); if (selected.value) void loadBoard(selected.value.instrumentId, board); }
function boardPeriodAvailable(board: Board, period: string): boolean { return board.availablePeriods.length === 0 || board.availablePeriods.includes(period); }
async function loadEarlierBoard(board: Board): Promise<void> {
  if (board.loading || board.loadingEarlier || !board.hasMore || !board.before || !board.instrumentId) return;
  const requestId = board.requestId; const instrumentId = board.instrumentId; const period = board.period; const previousBefore = board.before;
  board.loadingEarlier = true;
  try {
    const data = await apiGet<History>(`/api/market/instruments/${encodeURIComponent(instrumentId)}/bars/history`, { period, before: previousBefore, size: 60, version: marketVersion.value || undefined }, { ttlMs: 5 * 60_000, persist: true });
    if (requestId !== board.requestId || board.instrumentId !== instrumentId || board.period !== period || board.before !== previousBefore) return;
    board.bars = [...data.bars, ...board.bars]; board.start = data.start; board.total = data.total; board.before = data.before ?? data.bars[0]?.barOpenTime ?? data.bars[0]?.tradingDate ?? previousBefore; board.hasMore = data.hasMore ?? data.start > 0;
  } catch (reason) { if (requestId === board.requestId) error.value = reason instanceof Error ? reason.message : "更早行情加载失败"; } finally { if (requestId === board.requestId) board.loadingEarlier = false; }
}

function saveDrawings(): Promise<unknown> {
  const instrumentId = selected.value?.instrumentId; if (!instrumentId) return Promise.resolve();
  const path = `/api/market/instruments/${encodeURIComponent(instrumentId)}/drawings`;
  let snapshot: ChartDrawing[];
  try { snapshot = structuredClone(drawings.value); } catch { snapshot = JSON.parse(JSON.stringify(drawings.value)) as ChartDrawing[]; }
  cardDrawings.value = { ...cardDrawings.value, [instrumentId]: snapshot.filter((item) => item.crossPeriod || item.period === "1d") };
  drawingSaveChain = drawingSaveChain.catch(() => undefined).then(async () => {
    const result = await apiPut(path, { items: snapshot });
    invalidateQuery(path); return result;
  }).catch((reason) => { error.value = reason instanceof Error ? reason.message : "画线保存失败"; return undefined; });
  return drawingSaveChain;
}
function persistDrawingPreferences(): void {
  const styles = Object.fromEntries(Object.entries(drawingStyleDefaults.value).map(([type, style]) => {
    const { locked: _locked, ...persisted } = style;
    return [type, persisted];
  })) as Partial<Record<DrawingKind, ChartDrawingStyle>>;
  try { localStorage.setItem(drawingPreferenceKey, JSON.stringify({ magnet: magnet.value, crossPeriod: crossPeriod.value, keepDrawing: keepDrawing.value, styles } satisfies DrawingPreferences)); } catch { /* 浏览器禁用本地存储时仍保留当前会话默认值 */ }
}
function defaultDrawingStyle(type: DrawingKind): ChartDrawingStyle { return { ...baseDrawingStyle(type), ...drawingStyleDefaults.value[type], locked: false }; }
function rememberDrawingStyle(type: DrawingKind, style?: ChartDrawingStyle): void {
  if (!style) return;
  const { locked: _locked, ...persisted } = { ...defaultDrawingStyle(type), ...style };
  drawingStyleDefaults.value = { ...drawingStyleDefaults.value, [type]: persisted };
  persistDrawingPreferences();
}
function createDrawing(item: Omit<ChartDrawing, "id">, anchor?: { left: number; top: number }): void {
  const drawing: ChartDrawing = { ...item, id: `draw_${crypto.randomUUID()}`, period: history.value.period, crossPeriod: crossPeriod.value, style: defaultDrawingStyle(item.type) };
  drawings.value = [...drawings.value, drawing]; onSelectDrawing(drawing.id, anchor); if (!keepDrawing.value) tool.value = "cursor"; void saveDrawings();
}
function updateDrawing(item: ChartDrawing): void { drawings.value = drawings.value.map((drawing) => drawing.id === item.id ? item : drawing); selectedDrawingId.value = item.id; void saveDrawings(); }
function patchSelectedStyle(patch: Partial<ChartDrawingStyle>): void {
  const item = selectedDrawing.value; if (!item) return;
  const style = { ...defaultDrawingStyle(item.type), ...item.style, ...patch };
  if (!(Object.keys(patch).length === 1 && "locked" in patch)) rememberDrawingStyle(item.type, style);
  updateDrawing({ ...item, style });
}
function patchSelected(field: "text" | "crossPeriod", value: string | boolean): void {
  const item = selectedDrawing.value; if (!item) return;
  updateDrawing({ ...item, [field]: value });
  if (field === "crossPeriod") { crossPeriod.value = Boolean(value); persistDrawingPreferences(); }
}
function toggleDrawingPreference(field: "magnet" | "crossPeriod" | "keepDrawing"): void {
  if (field === "magnet") magnet.value = !magnet.value;
  else if (field === "crossPeriod") crossPeriod.value = !crossPeriod.value;
  else keepDrawing.value = !keepDrawing.value;
  persistDrawingPreferences();
}
function deleteSelectedDrawing(): void { const item = selectedDrawing.value; if (!item) return; rememberDrawingStyle(item.type, item.style); drawings.value = drawings.value.filter((drawing) => drawing.id !== item.id); selectedDrawingId.value = ""; void saveDrawings(); }
function deleteDrawings(): void { for (const item of drawings.value) rememberDrawingStyle(item.type, item.style); drawings.value = []; selectedDrawingId.value = ""; void saveDrawings(); }
function toggleIndicator(id: string): void { indicators.value = indicators.value.includes(id) ? indicators.value.filter((item) => item !== id) : [...indicators.value, id]; void loadHistory(); }
function toggleInverse(): void { inverse.value = !inverse.value; localStorage.setItem("market-chart-inverse", String(inverse.value)); }
function toggleColors(): void { swapColors.value = !swapColors.value; localStorage.setItem("market-chart-swap", String(swapColors.value)); }

async function loadHistory(): Promise<void> {
  if (!selected.value) return; historyAbort?.abort(); const controller = new AbortController(); historyAbort = controller; const request = ++serial; detailLoading.value = true; detailEarlierLoading.value = false; history.value.bars = [];
  try {
    const data = await apiGet<ChartBootstrap>(`/api/market/instruments/${encodeURIComponent(selected.value.instrumentId)}/chart`, { period: history.value.period, size: 60, indicators: indicators.value.join(","), version: marketVersion.value || undefined }, { ttlMs: 5 * 60_000, persist: true, signal: controller.signal });
    if (request !== serial) return;
    history.value = { ...data, before: data.before ?? data.bars[0]?.barOpenTime ?? data.bars[0]?.tradingDate, hasMore: data.hasMore ?? data.start > 0 }; indicatorValues.value = data.series; if (drawingsInstrumentId.value !== selected.value.instrumentId) { drawings.value = data.drawings; drawingsInstrumentId.value = selected.value.instrumentId; } selectedDrawingId.value = "";
    if (data.dataVersion) marketVersion.value = data.dataVersion;
  } catch (reason) { if (request === serial && !(reason instanceof DOMException && reason.name === "AbortError")) { error.value = reason instanceof Error ? reason.message : "K线加载失败"; history.value.bars = []; } } finally { if (request === serial) detailLoading.value = false; }
}
async function loadEarlierHistory(): Promise<void> {
  if (!selected.value || detailLoading.value || detailEarlierLoading.value || !history.value.hasMore || !history.value.before) return;
  const request = serial; const instrumentId = selected.value.instrumentId; const period = history.value.period; const previousBefore = history.value.before;
  detailEarlierLoading.value = true;
  try {
    const data = await apiGet<ChartBootstrap>(`/api/market/instruments/${encodeURIComponent(instrumentId)}/chart`, { period, before: previousBefore, size: 60, indicators: indicators.value.join(","), version: marketVersion.value || undefined }, { ttlMs: 5 * 60_000, persist: true });
    if (request !== serial || selected.value?.instrumentId !== instrumentId || history.value.period !== period || history.value.before !== previousBefore) return;
    const keys = new Set([...Object.keys(data.series), ...Object.keys(indicatorValues.value)]); const merged: Record<string, Array<number | null>> = {};
    for (const key of keys) merged[key] = [...(data.series[key] ?? Array(data.bars.length).fill(null)), ...(indicatorValues.value[key] ?? Array(history.value.bars.length).fill(null))];
    history.value = { ...history.value, start: data.start, size: data.bars.length + history.value.bars.length, total: data.total, before: data.before ?? data.bars[0]?.barOpenTime ?? data.bars[0]?.tradingDate ?? previousBefore, hasMore: data.hasMore ?? data.start > 0, earliestBarAt: data.earliestBarAt, bars: [...data.bars, ...history.value.bars] };
    indicatorValues.value = merged;
  } catch (reason) { if (request === serial) error.value = reason instanceof Error ? reason.message : "更早行情加载失败"; } finally { if (request === serial) detailEarlierLoading.value = false; }
}
function openWorkbench(item: Instrument): void {
  const changedInstrument = drawingsInstrumentId.value !== item.instrumentId;
  selected.value = item; fullscreen.value = true; history.value.period = "1d"; selectedDrawingId.value = "";
  if (changedInstrument) drawingsInstrumentId.value = "";
  void loadHistory(); void loadDrawings(item.instrumentId);
}
function switchPeriod(period: string): void { if (!history.value.availablePeriods.includes(period)) return; history.value.period = period; void loadHistory(); }

async function loadDrawings(instrumentId: string): Promise<void> {
  if (drawingsInstrumentId.value === instrumentId) return;
  // A rapid instrument round-trip can otherwise let this GET overtake an
  // already queued PUT and restore stale drawings into the reactive view.
  await drawingSaveChain.catch(() => undefined);
  try {
    const data = await apiGet<{ items: ChartDrawing[] }>(`/api/market/instruments/${encodeURIComponent(instrumentId)}/drawings`, undefined, { ttlMs: 5 * 60_000, persist: true, force: true });
    if (selected.value?.instrumentId !== instrumentId) return;
    drawings.value = data.items ?? [];
    drawingsInstrumentId.value = instrumentId;
  } catch { drawings.value = []; drawingsInstrumentId.value = instrumentId; }
}
function drawingsForPeriod(period: string): ChartDrawing[] {
  return drawings.value.filter((item) => item.crossPeriod || item.period === period).map((item) => ({ ...item, hidden: hiddenDrawings.value || item.hidden }));
}
watch(selected, () => { if (view.value === "list") { void loadBoards(); if (selected.value) void loadDrawings(selected.value.instrumentId); } });
onMounted(async () => { updateViewport(); window.addEventListener("resize", updateViewport); window.addEventListener("pointermove", resizePointer); window.addEventListener("pointerup", stopResize); await loadMarketVersion(); await Promise.all([loadCategories(), loadStrategies()]); await Promise.all([loadAll(), loadTargets()]); if (view.value === "list") await loadBoards(); });
onBeforeUnmount(() => { if (searchTimer) clearTimeout(searchTimer); allAbort?.abort(); historyAbort?.abort(); boardTop.value.abort?.abort(); boardBottom.value.abort?.abort(); window.removeEventListener("resize", updateViewport); window.removeEventListener("pointermove", resizePointer); window.removeEventListener("pointerup", stopResize); document.body.classList.remove("market-resizing"); });
</script>

<template>
  <main class="market-page">
    <section class="target-section">
      <div class="section-heading"><h1 class="page-title">目标行情</h1></div>
      <div class="target-filters">
        <div class="filter-row"><span>市场</span><nav class="target-nav" aria-label="目标行情市场筛选"><button v-for="item in categories" :key="item.id" type="button" :class="{ active: item.id === 'all' ? selectedTargetMarkets.length === 0 : selectedTargetMarkets.includes(item.id) }" :aria-pressed="item.id === 'all' ? selectedTargetMarkets.length === 0 : selectedTargetMarkets.includes(item.id)" @click="toggleTargetMarket(item.id)">{{ item.label }}</button></nav></div>
        <div class="filter-row"><span>策略</span><nav class="target-nav" aria-label="目标行情策略筛选"><button type="button" :class="{ active: selectedTargetStrategies.length === 0 }" :aria-pressed="selectedTargetStrategies.length === 0" @click="toggleTargetStrategy('all')">全部策略</button><button v-for="strategy in strategies" :key="strategy.strategyId" type="button" :class="{ active: selectedTargetStrategies.includes(strategy.strategyId) }" :aria-pressed="selectedTargetStrategies.includes(strategy.strategyId)" @click="toggleTargetStrategy(strategy.strategyId)">{{ strategy.displayName }}</button></nav></div>
      </div>
      <div v-loading="targetLoading" class="target-rows">
        <button v-for="item in targetItems" :key="item.instrumentId" type="button" class="target-row" @click="openWorkbench(item)"><b>{{ item.symbol || item.instrumentId }}</b><span>{{ item.name || "—" }}</span><strong>{{ noData(item.latestPrice ?? item.lastClose, 4) }}</strong></button>
        <p v-if="!strategies.length && !targetLoading" class="muted">暂无已保存策略。</p><p v-else-if="!targetItems.length && !targetLoading" class="muted">暂无同时满足当前市场与策略筛选的标的。</p>
      </div>
    </section>

    <section class="all-section">
      <div class="section-heading"><h2>全部行情</h2></div>
      <div class="all-toolbar">
        <el-select v-model="category" placeholder="市场类型" @change="resetAll"><el-option v-for="item in categories" :key="item.id" :label="item.label" :value="item.id" /></el-select>
        <el-input v-model="query" clearable placeholder="查询代码或名称" @input="search" @keyup.enter="searchNow" @clear="resetAll" />
        <el-button type="primary" @click="searchNow">查询</el-button>
        <div class="view-switch"><button type="button" :class="{ active: view === 'card' }" @click="persistView('card')">卡片视图</button><button type="button" :class="{ active: view === 'list' }" @click="persistView('list')">列表视图</button></div>
      </div>
      <el-alert v-if="error" :title="error" type="warning" :closable="false" class="page-alert" />
      <div v-if="view === 'card'" v-loading="loading" class="quote-grid">
        <article v-for="item in allItems" :key="item.instrumentId" class="quote-card"><button type="button" class="quote-summary" @click="openWorkbench(item)"><dl><div v-for="[field, text] in quoteFields" :key="field"><dt>{{ text }}</dt><dd>{{ quoteValue(item, field) }}</dd></div></dl></button><MiniKLine :instrument-id="item.instrumentId" :data-version="marketVersion" :drawings="cardDrawings[item.instrumentId] || []" :total-market-cap="item.totalMarketCap" :float-market-cap="item.floatMarketCap" :future-units="item.assetType === 'FUTURE'" /></article>
      </div>
      <div v-else class="list-workbench" v-loading="loading" :style="{ gridTemplateColumns: workbenchGridTemplate }">
        <div class="instrument-list" @scroll.passive="listScroll" @wheel.prevent="listWheel">
          <div class="list-header-row">
            <span class="flag-column-title">标记</span><span class="sequence-column-title">序号</span>
            <div class="list-table-header" :style="{ gridTemplateColumns: listGridTemplate }">
              <div v-for="column in listColumns" :key="column.id" class="column-header" draggable="true" :class="{ dragging: draggingColumn === column.id }" :title="`拖动调整${column.label}列顺序`" @dragstart="draggingColumn = column.id" @dragover.prevent @drop.prevent="reorderColumn(column.id)" @dragend="draggingColumn = undefined">
                <span>{{ column.label }}</span>
                <button type="button" class="column-resizer" role="separator" aria-orientation="vertical" :aria-label="`调整${column.label}列宽`" :aria-valuenow="columnWidths[column.id]" aria-valuemin="64" aria-valuemax="360" tabindex="0" @pointerdown="startColumnResize($event, column.id)" @keydown.left.prevent="resizeByKeyboard(column.id, -1)" @keydown.right.prevent="resizeByKeyboard(column.id, 1)" />
              </div>
            </div><span />
          </div>
          <div v-for="(item, index) in allItems" :key="item.instrumentId" class="instrument-row" :class="{ active: selected?.instrumentId === item.instrumentId, flagged: Boolean(rowFlags[item.instrumentId]) }" :style="rowFlagStyle(item)">
            <div class="flag-cell"><button type="button" class="row-flag" :class="{ marked: Boolean(rowFlags[item.instrumentId]) }" :style="{ color: rowFlags[item.instrumentId] || undefined }" :aria-label="`标记${item.name || item.symbol || item.instrumentId}`" :title="rowFlags[item.instrumentId] ? '更改行标记颜色' : '标记并选择行颜色'" @click.stop="toggleFlagPicker(item.instrumentId)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3v18M7 4h10l-2.3 4L17 12H7z" /></svg></button><div v-if="flagPickerInstrumentId === item.instrumentId" class="flag-palette" role="menu" aria-label="选择行标记颜色" @click.stop><button v-for="choice in flagChoices" :key="choice.label" type="button" role="menuitem" :aria-label="choice.label" :title="choice.label" :class="{ clear: !choice.color }" :style="choice.color ? { backgroundColor: choice.color } : undefined" @click="setRowFlag(item.instrumentId, choice.color)">{{ choice.color ? '' : '×' }}</button></div></div>
            <span class="sequence-cell">{{ index + 1 }}</span><button type="button" class="row-main" :style="{ gridTemplateColumns: listGridTemplate }" title="单击切换双看板，双击打开详情 K 线" @click="choose(item)" @dblclick="openWorkbench(item)"><span v-for="column in listColumns" :key="column.id" :class="`column-${column.id}`">{{ quoteValue(item, column.id) }}</span></button><button type="button" class="row-detail" aria-label="打开详情 K 线" title="打开详情 K 线" @click="openWorkbench(item)">↗</button>
          </div>
          <div v-if="hasMore" class="list-loading">{{ loading ? "正在加载…" : "向下滚动加载更多" }}</div>
        </div>
        <button type="button" class="workbench-resizer" role="separator" aria-orientation="vertical" aria-label="调整行情列表与K线图宽度" :aria-valuenow="Math.round(chartWidthVw)" aria-valuemin="25" aria-valuemax="75" title="左右拖动调整K线图宽度" @pointerdown="startSplitResize" @keydown.left.prevent="resizeSplitByKeyboard(1)" @keydown.right.prevent="resizeSplitByKeyboard(-1)"><span /></button>
        <div class="boards"><section v-loading="boardTop.loading" class="board"><header><button type="button" class="board-title" @click="selected && openWorkbench(selected)">{{ selected?.name || "请选择标的" }} <span>↗</span></button><nav aria-label="上看板 K 线周期"><button v-for="[id, text] in periodOptions" :key="id" type="button" :class="{ active: boardTop.period === id, unavailable: !boardPeriodAvailable(boardTop, id) }" :disabled="!boardPeriodAvailable(boardTop, id)" @click="boardPeriod('top', id)">{{ text }}</button></nav></header><KLineChart :bars="boardTop.bars" :period="boardTop.period" :height="boardChartHeight" :loading-earlier="boardTop.loadingEarlier" :drawings="drawingsForPeriod(boardTop.period)" :total-market-cap="selected?.totalMarketCap" :float-market-cap="selected?.floatMarketCap" :future-units="selected?.assetType === 'FUTURE'" show-quote-panel drawings-read-only @request-earlier="loadEarlierBoard(boardTop)" /></section><section v-loading="boardBottom.loading" class="board"><header><button type="button" class="board-title" @click="selected && openWorkbench(selected)">{{ selected?.name || "请选择标的" }} <span>↗</span></button><nav aria-label="下看板 K 线周期"><button v-for="[id, text] in periodOptions" :key="id" type="button" :class="{ active: boardBottom.period === id, unavailable: !boardPeriodAvailable(boardBottom, id) }" :disabled="!boardPeriodAvailable(boardBottom, id)" @click="boardPeriod('bottom', id)">{{ text }}</button></nav></header><KLineChart :bars="boardBottom.bars" :period="boardBottom.period" :height="boardChartHeight" :loading-earlier="boardBottom.loadingEarlier" :drawings="drawingsForPeriod(boardBottom.period)" :total-market-cap="selected?.totalMarketCap" :float-market-cap="selected?.floatMarketCap" :future-units="selected?.assetType === 'FUTURE'" show-quote-panel drawings-read-only @request-earlier="loadEarlierBoard(boardBottom)" /></section></div>
      </div>
      <el-pagination v-if="view === 'card' && allTotal > 0" class="market-pagination" layout="sizes, prev, pager, next, jumper, total" :page-sizes="[10,20,30,50,100]" :total="allTotal" :page-size="cardPageSize" :current-page="page" @size-change="changeCardPageSize" @current-change="void loadAll(false, $event)" />
    </section>

    <Teleport to="body"><div v-if="fullscreen" class="workbench-overlay" @pointerdown="dismissDrawingPopover"><header class="workbench-header"><div><b>{{ selected?.symbol || selected?.instrumentId }}</b><strong>{{ selected?.name }}</strong><span>{{ marketText(selected || {}) }} · 行情截止 {{ history.latestBarAt?.replace('T', ' ').slice(0, 19) || '—' }}</span></div><div class="workbench-actions"><el-button-group><el-button :type="inverse ? 'primary' : 'default'" @click="toggleInverse">坐标反转</el-button><el-button :type="swapColors ? 'primary' : 'default'" @click="toggleColors">涨跌换色</el-button></el-button-group><el-dropdown><el-button>指标</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item v-for="id in ['ma','hsar','sd','bollinger','atr','volume']" :key="id" @click="toggleIndicator(id)">{{ indicators.includes(id) ? '✓ ' : '' }}{{ { ma:'MA', hsar:'HSAR', sd:'SD', bollinger:'布林带', atr:'ATR通道', volume:'成交量' }[id] }}</el-dropdown-item></el-dropdown-menu></template></el-dropdown><button class="close-workbench" type="button" aria-label="关闭图表" @click="fullscreen = false">×</button></div></header>
      <aside class="drawing-toolbar">
        <button v-for="item in drawingTools" :key="item.id" type="button" :class="{ active: tool === item.id }" :aria-label="item.label" :title="item.label" @click="selectDrawingTool(item.id)"><svg viewBox="0 0 24 24" aria-hidden="true"><path :d="item.path" /></svg></button>
        <hr>
        <button type="button" :class="{ active: magnet }" aria-label="吸附" title="吸附" @click="toggleDrawingPreference('magnet')"><svg viewBox="0 0 24 24"><g transform="rotate(-35 12 12)"><path d="M6 3v8a6 6 0 0012 0V3h-4v8a2 2 0 01-4 0V3zM6 7h4M14 7h4" /></g></svg></button>
        <button type="button" :class="{ active: crossPeriod }" aria-label="跨周期" title="跨周期" @click="toggleDrawingPreference('crossPeriod')"><svg viewBox="0 0 24 24"><path d="M4 20v-4h3v4M9 20v-7h3v7M14 20v-10h3v10M19 20V7h2v13M3 12c4-1 7-4 10-4s5-3 8-5" /></svg></button>
        <button type="button" :class="{ active: keepDrawing }" aria-label="连续画线" title="连续画线" @click="toggleDrawingPreference('keepDrawing')"><svg viewBox="0 0 24 24"><path d="M4 17l5-5 4 3 7-8M17 7h3v3" /></svg></button>
        <button type="button" :class="{ active: hiddenDrawings }" aria-label="隐藏画线" title="隐藏画线" @click="hiddenDrawings = !hiddenDrawings"><svg viewBox="0 0 24 24"><path d="M3 12s3-5 9-5 9 5 9 5-3 5-9 5-9-5-9-5z" /><circle cx="12" cy="12" r="2.4" /><path v-if="hiddenDrawings" d="M4 4l16 16" /></svg></button>
        <button type="button" aria-label="删除全部画线" title="删除全部画线" @click="deleteDrawings"><svg viewBox="0 0 24 24"><path d="M5 7h14M9 7V4h6v3M8 10v8M12 10v8M16 10v8M7 7l1 14h8l1-14" /></svg></button>
      </aside>
      <nav class="period-bar" aria-label="详情 K 线周期"><button v-for="[id, text] in periodOptions" :key="id" type="button" :class="{ active: history.period === id, unavailable: !history.availablePeriods.includes(id) }" :disabled="!history.availablePeriods.includes(id)" :title="history.availablePeriods.includes(id) ? `${text} 周期` : '本地暂无该周期数据'" @click="switchPeriod(id)">{{ text }}</button></nav>
      <section ref="workbenchChart" v-loading="detailLoading" class="workbench-chart">
        <div v-if="selectedDrawing" ref="drawingPopoverElement" class="drawing-popover" :style="drawingPopoverStyle">
          <button class="popover-drag-handle" type="button" aria-label="拖动工具栏" title="拖动工具栏" @pointerdown.stop="startDrawingPopoverDrag" @pointermove="moveDrawingPopover" @pointerup="stopDrawingPopoverDrag" @pointercancel="stopDrawingPopoverDrag"><svg viewBox="0 0 16 18"><circle v-for="index in 6" :key="index" :cx="index % 2 ? 5 : 11" :cy="4 + Math.floor((index - 1) / 2) * 5" r="1" /></svg></button>
          <DrawingColorPicker :model-value="selectedDrawing.style?.color || '#2196f3'" :title="selectedDrawing.type === 'text' ? '文字颜色' : '线条颜色'" :presets="drawingColorPresets" @update:model-value="patchSelectedStyle({ color: $event })" />
          <template v-if="selectedDrawing.type !== 'text'">
            <details class="preview-select" title="线宽"><summary aria-label="选择线宽"><span class="line-width-preview" :style="{ height: `${selectedDrawing.style?.width || 1.5}px` }" /></summary><div><button v-for="value in [1,1.5,2,3,4]" :key="value" type="button" :aria-label="`${value}像素线宽`" :title="`${value}像素线宽`" @click="patchSelectedStyle({ width: value }); closePreviewSelect($event)"><span class="line-width-preview" :style="{ height: `${value}px` }" /></button></div></details>
            <details class="preview-select" title="线型"><summary aria-label="选择线型"><span class="line-style-preview" :class="`line-style-${selectedDrawing.style?.lineStyle || 'solid'}`" /></summary><div><button v-for="[value,text] in lineStyles" :key="value" type="button" :aria-label="text" :title="text" @click="patchSelectedStyle({ lineStyle: value }); closePreviewSelect($event)"><span class="line-style-preview" :class="`line-style-${value}`" /></button></div></details>
          </template>
          <DrawingColorPicker v-if="selectedDrawing.type === 'rectangle'" :model-value="selectedDrawing.style?.fillColor || '#2196f3'" checkerboard title="箱体填充颜色" :presets="drawingColorPresets" @update:model-value="patchSelectedStyle({ fillColor: $event, fillOpacity: 1 })" />
          <select v-if="selectedDrawing.type === 'text'" class="font-size-select" :value="selectedDrawing.style?.fontSize || 14" aria-label="字号" title="字号" @change="patchSelectedStyle({ fontSize: Number(($event.target as HTMLSelectElement).value) })"><option v-for="size in [10,12,14,16,18,20,24,28,32,40,48,56,64,72]" :key="size" :value="size">{{ size }}px</option></select>
          <button type="button" class="icon-action" :class="{ active: selectedDrawing.style?.locked }" :aria-label="selectedDrawing.style?.locked ? '解除锁定' : '锁定'" :title="selectedDrawing.style?.locked ? '解除锁定' : '锁定'" @click="patchSelectedStyle({ locked: !selectedDrawing.style?.locked })"><svg viewBox="0 0 24 24"><path :d="selectedDrawing.style?.locked ? 'M7 10V7a5 5 0 0110 0v3M6 10h12v11H6zM12 14v3' : 'M8 10V7a4 4 0 018 0M6 10h12v11H6zM12 14v3'" /></svg></button>
          <button type="button" class="icon-action" :class="{ active: selectedDrawing.crossPeriod }" aria-label="跨周期" title="跨周期" @click="patchSelected('crossPeriod', !selectedDrawing.crossPeriod)"><svg viewBox="0 0 24 24"><path d="M4 20v-4h3v4M9 20v-7h3v7M14 20v-10h3v10M19 20V7h2v13M3 12c4-1 7-4 10-4s5-3 8-5" /></svg></button>
          <button type="button" class="icon-action danger" aria-label="删除" title="删除" @click="deleteSelectedDrawing"><svg viewBox="0 0 24 24"><path d="M5 7h14M9 7V4h6v3M8 10v8M12 10v8M16 10v8M7 7l1 14h8l1-14" /></svg></button>
        </div>
        <KLineChart :bars="history.bars" :period="history.period" :height="detailChartHeight" :indicators="indicatorValues" :inverse="inverse" :swap-colors="swapColors" :drawings="visibleDrawings" :drawing-tool="tool" :magnet="magnet" :selected-drawing-id="selectedDrawingId" :total-market-cap="selected?.totalMarketCap" :float-market-cap="selected?.floatMarketCap" :future-units="selected?.assetType === 'FUTURE'" :loading-earlier="detailEarlierLoading" show-quote-panel @draw="createDrawing" @select-drawing="onSelectDrawing" @update-drawing="updateDrawing" @request-earlier="loadEarlierHistory" />
      </section>
    </div></Teleport>
  </main>
</template>

<style scoped>
.market-page{width:96vw;margin:0 2vw}.target-section,.all-section{padding:0;border:0;background:transparent}.all-section{margin-top:28px}.section-heading{margin-bottom:14px}.section-heading h1,.section-heading h2{margin:0;font-size:clamp(22px,2vw,30px);letter-spacing:-.03em}.target-filters{display:flex;flex-direction:column;gap:9px;margin-bottom:14px}.filter-row{display:grid;grid-template-columns:42px 1fr;gap:8px;align-items:start}.filter-row>span{padding-top:7px;color:var(--ml-text-disabled);font-size:11px}.target-nav{display:flex;flex-wrap:wrap;gap:6px}.target-nav button{border:1px solid var(--ml-divider);border-radius:6px;padding:6px 10px;background:var(--ml-surface);color:var(--ml-text-secondary);cursor:pointer}.target-nav button.active{border-color:var(--ml-accent);background:var(--ml-surface-selected);color:var(--ml-text-primary)}.target-rows{display:flex;flex-wrap:wrap;gap:8px;min-height:42px}.target-row{display:grid;grid-template-columns:auto 1fr auto;gap:9px;align-items:center;min-width:260px;padding:10px 12px;border:1px solid var(--ml-divider);border-radius:8px;background:var(--ml-surface);color:var(--ml-text-primary);cursor:pointer;text-align:left}.target-row:hover{border-color:var(--ml-accent)}.target-row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ml-text-secondary)}
.all-toolbar{position:sticky;top:52px;z-index:30;display:grid;grid-template-columns:240px minmax(180px,1fr) auto auto;gap:10px;padding:12px 0;background:var(--ml-background)}.view-switch{display:flex;padding:3px;border:1px solid var(--ml-divider);border-radius:8px;background:var(--ml-surface)}.view-switch button{border:0;background:transparent;padding:0 12px;color:var(--ml-text-secondary);cursor:pointer}.view-switch button.active{background:var(--ml-surface-selected);color:var(--ml-text-primary);border-radius:5px}.quote-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.quote-card{border:1px solid var(--ml-divider);border-radius:9px;background:var(--ml-surface);color:var(--ml-text-primary);overflow:hidden}.quote-card:hover{border-color:var(--ml-accent)}.quote-summary{display:block;width:100%;border:0;background:transparent;color:inherit;cursor:pointer;text-align:left}.quote-card dl{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));margin:0;padding:12px;gap:9px}.quote-card dl>div{min-width:0}.quote-card dt{margin-bottom:4px;color:var(--ml-text-secondary);font-size:11px;white-space:nowrap}.quote-card dd{overflow:hidden;margin:0;font:700 12px/1.35 ui-monospace,Consolas,monospace;text-overflow:ellipsis;white-space:nowrap}.list-workbench{display:grid;grid-template-columns:minmax(560px,50%) 1fr;height:calc(100vh - 118px);min-height:600px;border:1px solid var(--ml-divider);border-radius:2px;overflow:hidden;background:var(--ml-surface)}.instrument-list{height:100%;overflow-y:auto;overscroll-behavior:contain;border-right:1px solid var(--ml-divider)}.list-header-row,.instrument-row{display:grid;grid-template-columns:1fr 30px;min-width:560px;border-bottom:1px solid var(--ml-divider)}.list-header-row{position:sticky;top:0;z-index:4;background:var(--ml-surface-elevated)}.list-table-header,.row-main{display:grid;align-items:center;min-width:0}.list-table-header button{height:28px;overflow:hidden;border:0;border-right:1px solid var(--ml-divider);background:transparent;color:var(--ml-text-secondary);cursor:grab;font-size:11px;text-align:left;text-overflow:ellipsis;white-space:nowrap}.list-table-header button.dragging{opacity:.45}.list-table-header button span{float:right;color:var(--ml-text-disabled)}.instrument-row.active{background:var(--ml-surface-selected)}.row-main{border:0;padding:0;background:transparent;color:var(--ml-text-primary);cursor:pointer;text-align:left}.row-main>span{overflow:hidden;padding:5px 6px;border-right:1px solid color-mix(in srgb,var(--ml-divider) 60%,transparent);color:var(--ml-text-secondary);font-size:12px;text-overflow:ellipsis;white-space:nowrap}.row-main .column-symbol,.row-main .column-close{color:var(--ml-text-primary);font-family:ui-monospace,Consolas,monospace;font-weight:700}.row-detail{border:0;background:transparent;color:var(--ml-text-disabled);cursor:pointer}.row-detail:hover{color:var(--ml-accent)}.list-loading{padding:12px;text-align:center;color:var(--ml-text-disabled);font-size:11px}.boards{display:grid;grid-template-rows:1fr 1fr;min-width:0;min-height:0}.board{min-height:0;padding:0;border-bottom:1px solid var(--ml-divider);overflow:hidden}.board:last-child{border-bottom:0}.board header{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:6px;min-height:30px;padding:0 4px}.board-title{border:0;background:transparent;color:var(--ml-text-primary);font-weight:650;cursor:pointer;white-space:nowrap}.board-title span{color:var(--ml-accent)}.board nav{display:flex;justify-content:flex-end;gap:1px;overflow-x:auto}.board nav button,.period-bar button{flex:0 0 auto;border:0;border-radius:3px;background:transparent;color:var(--ml-text-secondary);padding:3px 5px;cursor:pointer;font-size:11px}.board nav button.active,.period-bar button.active{background:var(--ml-surface-selected);color:var(--ml-text-primary)}.board nav button.unavailable{opacity:.32;cursor:not-allowed}.market-pagination{justify-content:flex-end;margin-top:16px}
:global(.workbench-overlay){position:fixed;inset:0;z-index:3000;background:var(--ml-background);color:var(--ml-text-primary);display:grid;grid-template-columns:48px 1fr;grid-template-rows:56px 42px 1fr}.workbench-header{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 16px;border-bottom:1px solid var(--ml-divider);background:var(--ml-surface)}.workbench-header>div:first-child{display:flex;align-items:baseline;gap:10px;min-width:0}.workbench-header strong{font-size:18px}.workbench-header span{overflow:hidden;color:var(--ml-text-secondary);font-size:12px;text-overflow:ellipsis;white-space:nowrap}.workbench-actions{display:flex;align-items:center;gap:8px}.close-workbench{border:0;background:transparent;color:var(--ml-text-primary);font-size:32px;line-height:1;cursor:pointer}.drawing-toolbar{grid-row:2/4;display:flex;flex-direction:column;align-items:center;gap:5px;padding:7px 5px;border-right:1px solid var(--ml-divider);background:var(--ml-surface);overflow:auto}.drawing-toolbar button{display:grid;place-items:center;width:34px;height:34px;border:1px solid transparent;border-radius:6px;background:transparent;color:var(--ml-text-secondary);cursor:pointer}.drawing-toolbar button:hover,.drawing-toolbar button.active{border-color:var(--ml-accent);background:var(--ml-surface-selected);color:var(--ml-text-primary)}.drawing-toolbar svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}.drawing-toolbar hr{width:26px;border:0;border-top:1px solid var(--ml-divider)}.period-bar{display:flex;align-items:center;gap:3px;overflow:auto;padding:5px 12px;border-bottom:1px solid var(--ml-divider)}.period-bar button{padding:5px 9px}.period-bar button.unavailable{opacity:.32;cursor:not-allowed}.workbench-chart{position:relative;min-width:0;min-height:0;overflow:hidden}.drawing-popover{position:absolute;z-index:12;top:90px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:7px;max-width:calc(100% - 24px);padding:6px 8px;border:1px solid var(--ml-divider);border-radius:8px;background:color-mix(in srgb,var(--ml-surface) 94%,transparent);box-shadow:0 8px 24px rgba(0,0,0,.24);overflow-x:auto;font-size:11px}.drawing-popover label{display:flex;align-items:center;gap:4px;white-space:nowrap;color:var(--ml-text-secondary)}.drawing-popover input[type=color]{width:26px;height:24px;padding:1px;border:1px solid var(--ml-divider);background:transparent}.drawing-popover input[type=text]{width:100px}.drawing-popover input[type=number]{width:54px}.drawing-popover select,.drawing-popover input[type=text],.drawing-popover input[type=number]{height:25px;border:1px solid var(--ml-divider);border-radius:4px;background:var(--ml-background);color:var(--ml-text-primary)}.drawing-popover button{height:26px;border:1px solid var(--ml-divider);border-radius:5px;background:var(--ml-background);color:var(--ml-text-secondary);cursor:pointer;white-space:nowrap}.drawing-popover button.active{border-color:var(--ml-accent);color:var(--ml-text-primary)}.drawing-popover button.danger{color:var(--ml-error)}
.quote-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.list-workbench{grid-template-columns:none}.instrument-list{overflow:auto;border-right:0}.list-header-row,.instrument-row{min-width:0}.list-table-header>button{display:none}.column-header{position:relative;height:28px;overflow:hidden;padding:0 12px 0 6px;border-right:1px solid var(--ml-divider);color:var(--ml-text-secondary);cursor:grab;font-size:11px;line-height:28px;text-overflow:ellipsis;white-space:nowrap}.column-header.dragging{opacity:.45}.column-resizer{position:absolute;z-index:2;top:0;right:-4px;width:9px;height:100%;padding:0;border:0;background:transparent;cursor:col-resize}.column-resizer::after{position:absolute;top:5px;bottom:5px;left:4px;width:1px;background:var(--ml-divider);content:""}.column-resizer:hover::after,.column-resizer:focus-visible::after{width:2px;background:var(--ml-accent)}.workbench-resizer{position:relative;z-index:6;width:8px;height:100%;padding:0;border:0;border-right:1px solid var(--ml-divider);border-left:1px solid var(--ml-divider);background:var(--ml-surface-elevated);cursor:col-resize}.workbench-resizer span{position:absolute;top:50%;left:1px;width:4px;height:42px;transform:translateY(-50%);border-radius:3px;background:var(--ml-divider)}.workbench-resizer:hover span,.workbench-resizer:focus-visible span{background:var(--ml-accent)}:global(body.market-resizing){cursor:col-resize;user-select:none}.line-options{display:flex;align-items:center;gap:3px;margin:0;padding:0;border:0}.line-options legend{float:left;margin-right:2px;color:var(--ml-text-secondary);white-space:nowrap}.line-options button{display:grid;place-items:center;width:34px;padding:0}.line-width-preview,.line-style-preview{display:block;width:24px;min-height:1px;background:currentColor}.line-style-preview{height:2px}.line-style-dashed{background:repeating-linear-gradient(90deg,currentColor 0 7px,transparent 7px 11px)}.line-style-dotted{background:repeating-linear-gradient(90deg,currentColor 0 2px,transparent 2px 5px)}.line-style-dashdot{background:repeating-linear-gradient(90deg,currentColor 0 9px,transparent 9px 12px,currentColor 12px 14px,transparent 14px 18px)}
.market-page{box-sizing:border-box;max-width:100%;overflow-x:clip}.target-section,.all-section{min-width:0}.list-workbench{grid-template-columns:minmax(0,50%) 1fr;min-width:0}.instrument-list{position:relative;min-width:0;min-height:0;overflow-x:hidden;overflow-y:auto;scrollbar-gutter:stable}.list-header-row,.instrument-row{display:grid;grid-template-columns:34px 42px minmax(0,1fr) 30px;min-width:0}.list-header-row{z-index:8}.flag-column-title,.sequence-column-title,.sequence-cell{display:grid;place-items:center;border-right:1px solid var(--ml-divider);color:var(--ml-text-secondary);font-size:11px}.list-table-header,.row-main{min-width:0;overflow:hidden}.instrument-row{position:relative;background:var(--ml-surface)}.instrument-row.flagged{background:color-mix(in srgb,var(--row-flag-color) 22%,var(--ml-surface))}.instrument-row.active{box-shadow:inset 3px 0 var(--ml-accent)}.instrument-row.active.flagged{background:color-mix(in srgb,var(--row-flag-color) 34%,var(--ml-surface-selected))}.flag-cell{position:relative;display:grid;place-items:center;border-right:1px solid var(--ml-divider)}.row-flag{display:grid;place-items:center;width:26px;height:24px;padding:0;border:0;background:transparent;color:var(--ml-text-disabled);cursor:pointer}.row-flag svg{width:15px;height:15px;fill:transparent;stroke:currentColor;stroke-width:1.8}.row-flag.marked svg{fill:currentColor}.flag-palette{position:absolute;z-index:20;top:24px;left:3px;display:flex;gap:4px;padding:5px;border:1px solid var(--ml-divider);border-radius:6px;background:var(--ml-surface-elevated);box-shadow:0 6px 18px rgba(0,0,0,.25)}.flag-palette button{width:20px;height:20px;padding:0;border:1px solid color-mix(in srgb,var(--ml-text-primary) 28%,transparent);border-radius:50%;cursor:pointer}.flag-palette button.clear{display:grid;place-items:center;background:var(--ml-surface);color:var(--ml-text-secondary);font-weight:700}
.list-table-header .column-resizer{cursor:col-resize}
.drawing-popover{overflow:visible}.popover-drag-handle{display:grid!important;place-items:center;width:27px!important;padding:0!important;border:0!important;background:transparent!important;cursor:move!important;touch-action:none}.popover-drag-handle svg{width:16px;height:18px;fill:var(--ml-text-secondary)}.popover-color{width:28px;height:26px;padding:1px;border:1px solid var(--ml-divider);border-radius:4px;background:transparent;cursor:pointer}.popover-opacity{width:70px}.font-size-select{width:70px}.preview-select{position:relative}.preview-select summary{display:grid;place-items:center;width:40px;height:26px;border:1px solid var(--ml-divider);border-radius:5px;background:var(--ml-background);color:var(--ml-text-primary);cursor:pointer;list-style:none}.preview-select summary::-webkit-details-marker{display:none}.preview-select>div{position:absolute;z-index:20;top:30px;left:0;display:grid;gap:3px;padding:4px;border:1px solid var(--ml-divider);border-radius:5px;background:var(--ml-surface);box-shadow:0 6px 18px rgba(0,0,0,.25)}.preview-select button{display:grid;place-items:center;width:40px!important;padding:0!important}.drawing-popover .icon-action{display:grid;place-items:center;width:29px;padding:0}.drawing-popover .icon-action svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
@media (max-width:1280px){.quote-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (max-width:800px){.market-page{width:calc(100vw - 24px);margin:0 12px}.all-toolbar{grid-template-columns:1fr;position:static}.quote-grid{grid-template-columns:1fr}.list-workbench{grid-template-columns:1fr!important;grid-template-rows:240px 1fr;height:auto;min-height:900px}.workbench-resizer{display:none}.instrument-list{border-right:0;border-bottom:1px solid var(--ml-divider)}.workbench-header span,.workbench-actions .el-button-group,.workbench-actions>.el-dropdown{display:none}.drawing-popover{left:8px;right:8px;transform:none}}
.quote-card dl{padding:6px 10px 5px;gap:7px}.quote-card dt{margin-bottom:2px;font-size:10px}.quote-card dd{font:700 11px/1.3 ui-monospace,Consolas,monospace}.drawing-popover{gap:5px;padding:4px 6px;border-radius:6px}.drawing-popover button{height:24px}.drawing-popover .icon-action{width:26px}.drawing-popover .popover-drag-handle{width:24px!important}
</style>
