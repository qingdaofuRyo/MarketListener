<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";

export interface KLineBar {
  barOpenTime?: string; tradingDate?: string; open?: number; high?: number; low?: number; close?: number;
  volume?: number; amount?: number; turnoverRate?: number; openInterest?: number; settlement?: number;
  change?: number; pctChange?: number; amplitude?: number; capitalDeposit?: number; capitalDepositReason?: string;
}
export type DrawingTool = "cursor" | "horizontal" | "vertical" | "rectangle" | "text" | "brush";
export type DrawingLineStyle = "solid" | "dashed" | "dotted" | "dashdot";
export interface ChartDrawingStyle {
  color?: string; width?: number; lineStyle?: DrawingLineStyle; fillColor?: string; fillOpacity?: number;
  fontSize?: number; borderColor?: string; borderWidth?: number; borderStyle?: DrawingLineStyle; locked?: boolean;
}
export interface ChartDrawingPoint {
  time: string; price: number;
}
export interface ChartDrawing {
  id: string; type: Exclude<DrawingTool, "cursor">; period?: string; crossPeriod?: boolean; hidden?: boolean;
  text?: string; points: ChartDrawingPoint[]; style?: ChartDrawingStyle;
}
const DEFAULT_DRAWING_COLOR = "#2196f3";
const RECTANGLE_LABEL_FONT = '12px "Microsoft YaHei UI","Microsoft YaHei",sans-serif';
const RECTANGLE_LABEL_HEIGHT = 42;
const RECTANGLE_LABEL_GAP = 4;
interface DrawingAnchor { left: number; top: number }
interface RectangleDrag {
  drawingId: string;
  mode: "move" | "resize-start" | "resize-end";
  startX: number;
  originPoints: ChartDrawingPoint[];
  points: ChartDrawingPoint[];
}
interface BrushDraft { points: ChartDrawingPoint[]; lastX: number; lastY: number; anchor: DrawingAnchor }
interface PointerCaptureTarget extends EventTarget {
  setPointerCapture?: (pointerId: number) => void;
  releasePointerCapture?: (pointerId: number) => void;
}

const props = withDefaults(defineProps<{
  bars: KLineBar[]; height?: number; indicators?: Record<string, Array<number | null | undefined>>;
  inverse?: boolean; swapColors?: boolean; drawings?: ChartDrawing[]; drawingTool?: DrawingTool;
  magnet?: boolean; defaultVisible?: number; compact?: boolean; selectedDrawingId?: string;
  showQuotePanel?: boolean; totalMarketCap?: number; floatMarketCap?: number; loadingEarlier?: boolean; period?: string; drawingsReadOnly?: boolean;
  futureUnits?: boolean; brushStyle?: ChartDrawingStyle;
}>(), {
  bars: () => [], height: 460, indicators: () => ({}), inverse: false, swapColors: false,
  drawings: () => [], drawingTool: "cursor", magnet: false, defaultVisible: 60, compact: false,
  selectedDrawingId: "", showQuotePanel: false, totalMarketCap: undefined, floatMarketCap: undefined,
  loadingEarlier: false, period: "1d", drawingsReadOnly: false,
  futureUnits: false, brushStyle: () => ({}),
});
const emit = defineEmits<{
  hover: [bar: KLineBar | null]; draw: [drawing: Omit<ChartDrawing, "id">, anchor?: DrawingAnchor];
  visibleRange: [start: number, end: number]; selectDrawing: [id: string, anchor?: DrawingAnchor]; updateDrawing: [drawing: ChartDrawing];
  requestEarlier: [];
}>();
const theme = useThemeStore();
const element = ref<HTMLElement>();
const hoverIndex = ref(-1);
const range = ref({ start: 0, end: 0 });
let chart: echarts.ECharts | undefined;
const rectangleAnchor = ref<ChartDrawingPoint>();
const rectangleCursor = ref<ChartDrawingPoint>();
const textDraft = ref<{ point: { time: string; price: number }; left: number; top: number; value: string; editingId?: string }>();
const textInput = ref<HTMLInputElement>();
let panStartX: number | undefined;
let panOrigin: { start: number; end: number } | undefined;
let rectangleDrag: RectangleDrag | undefined;
let brushDraft: BrushDraft | undefined;
let brushPointerId: number | undefined;
let brushPointerTarget: PointerCaptureTarget | undefined;
let rectangleRenderFrame: number | undefined;
let textMeasureCanvas: HTMLCanvasElement | undefined;
let requestedEarlierInGesture = false;
let rangeRenderTimer: ReturnType<typeof setTimeout> | undefined;
let requestedEarlierForLength = -1;
let pendingEarlierShift = 0;
let resizeObserver: ResizeObserver | undefined;

const categories = computed(() => props.bars.map((bar) => String(bar.barOpenTime || bar.tradingDate || "")));
const hoverBar = computed(() => props.bars[hoverIndex.value] ?? props.bars.at(-1) ?? null);
const candleData = computed(() => props.bars.map((bar) => [asNumber(bar.open), asNumber(bar.close), asNumber(bar.low), asNumber(bar.high)]));
const volumeData = computed(() => props.bars.map((bar) => ({ value: asNumber(bar.volume) ?? 0, itemStyle: { color: upColor(bar) } })));
const secondaryMetric = computed<"openInterest" | "turnoverRate">(() => props.bars.some((bar) => asNumber(bar.openInterest) != null) ? "openInterest" : "turnoverRate");
const secondaryData = computed(() => props.bars.map((bar) => asNumber(bar[secondaryMetric.value])));
const majorTicks = computed(() => {
  const indexes = new Set<number>();
  let previous = "";
  categories.value.forEach((value, index) => {
    const day = value.slice(0, 10); const month = day.slice(0, 7); const year = day.slice(0, 4);
    const key = ["5m", "15m", "30m", "1h", "2h"].includes(props.period) ? day : ["1d", "1w"].includes(props.period) ? month : year;
    if (index === 0 || key !== previous) indexes.add(index);
    previous = key;
  });
  return indexes;
});
const quotePairs = computed(() => {
  const bar = hoverBar.value;
  const volumeText = props.futureUnits ? futureUnit(bar?.volume) : stockUnit(bar?.volume);
  const amountText = props.futureUnits ? futureUnit(bar?.amount) : stockUnit(bar?.amount);
  const openInterestText = props.futureUnits ? futureUnit(bar?.openInterest) : format(bar?.openInterest, 0);
  const depositText = props.futureUnits ? futureUnit(bar?.capitalDeposit) : stockUnit(bar?.capitalDeposit);
  return [
    [["开盘价", format(bar?.open)], ["收盘价", format(bar?.close)]],
    [["最高价", format(bar?.high)], ["最低价", format(bar?.low)]],
    [["结算价", format(bar?.settlement)], ["振幅", percent(bar?.amplitude)]],
    [["涨幅", percent(bar?.pctChange)], ["涨跌", format(bar?.change)]],
    [["成交量", volumeText], ["成交额", amountText]],
    [["持仓量", openInterestText], ["沉淀资金", depositText]],
    [["总市值", format(props.totalMarketCap, 2)], ["流通市值", format(props.floatMarketCap, 2)]],
  ];
});
const displayQuotePanel = computed(() => props.showQuotePanel || !props.compact);
const secondaryMetricLabel = computed(() => secondaryMetric.value === "openInterest" ? "持仓量" : "换手率");

function asNumber(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function format(value: unknown, digits = 4): string { const item = asNumber(value); return item == null ? "—" : item.toLocaleString("zh-CN", { maximumFractionDigits: digits }); }
function percent(value: unknown): string { const item = asNumber(value); return item == null ? "—" : `${item.toFixed(2)}%`; }
function stockUnit(value: unknown): string { const item = asNumber(value); return item == null ? "—" : `${(item / 1e8).toFixed(2)}亿`; }
function futureUnit(value: unknown): string { const item = asNumber(value); return item == null ? "—" : `${(item / 1e4).toFixed(2)}万`; }
function compactAxis(value: unknown): string {
  const item = asNumber(value);
  if (item == null) return "—";
  const abs = Math.abs(item);
  if (abs >= 1e8) return `${(item / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(item / 1e4).toFixed(2)}万`;
  return item.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function timeLabel(value: string): string { const text = value.replace("T", " "); return text.length >= 16 ? text.slice(0, 16) : `${text.slice(0, 10)} 00:00`; }
function axisTimeLabel(value: string): string {
  const day = value.slice(0, 10); if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return timeLabel(value);
  const weekday = new Intl.DateTimeFormat("zh-CN", { weekday: "short", timeZone: "Asia/Shanghai" }).format(new Date(`${day}T00:00:00+08:00`));
  return `${day.slice(5)} ${weekday}`;
}
function isMajorTick(index: number): boolean { return majorTicks.value.has(index); }
function upColor(bar: KLineBar): string { const up = (asNumber(bar.close) ?? 0) >= (asNumber(bar.open) ?? 0); const palette = theme.palette; return props.swapColors ? (up ? palette.priceDown : palette.priceUp) : (up ? palette.priceUp : palette.priceDown); }
function initialRange(): { start: number; end: number } { const total = props.bars.length; return { start: Math.max(0, total - Math.min(props.defaultVisible, total)), end: Math.max(0, total - 1) }; }
function priceBounds(): { min: number; max: number; span: number } {
  const windowBars = props.bars.slice(Math.max(0, range.value.start), Math.min(props.bars.length, range.value.end + 1));
  const values = windowBars.flatMap((bar) => [asNumber(bar.high), asNumber(bar.low)]).filter((value): value is number => value != null);
  const rawMin = values.length ? Math.min(...values) : 0; const rawMax = values.length ? Math.max(...values) : 1;
  const span = Math.max(Math.abs(rawMax - rawMin), Math.abs(rawMax || 1) * 0.01);
  const padding = props.compact ? 0.08 : 0.18;
  return { min: rawMin - span * padding, max: rawMax + span * padding, span };
}
function lineDash(value?: DrawingLineStyle): number[] | undefined {
  if (value === "dashed") return [8, 4];
  if (value === "dotted") return [2, 3];
  if (value === "dashdot") return [10, 5, 2, 5];
  return undefined;
}
function alpha(color: string, opacity: number): string {
  const value = color.replace("#", "");
  if (/^[0-9a-f]{6}$/i.test(value)) {
    const [red, green, blue] = [0, 2, 4].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16));
    return `rgba(${red},${green},${blue},${opacity})`;
  }
  return color;
}
function drawingPoint(item: { time: string; price: number }): number[] | null {
  if (!chart) return null;
  const index = nearestCategoryIndex(item.time);
  if (index < 0) return null;
  const pixel = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [index, item.price]);
  return Array.isArray(pixel) ? pixel.map(Number) : null;
}
function secondaryAxis(value: unknown): string { return secondaryMetric.value === "turnoverRate" ? `${format(value, 2)}%` : compactAxis(value); }
function nearestCategoryIndex(time: string): number {
  const exact = categories.value.indexOf(time);
  if (exact >= 0) return exact;
  const target = Date.parse(time);
  if (!Number.isFinite(target)) return -1;
  return categories.value.reduce((nearest, value, candidate) => {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return nearest;
    if (nearest < 0) return candidate;
    return Math.abs(timestamp - target) < Math.abs(Date.parse(categories.value[nearest]) - target) ? candidate : nearest;
  }, -1);
}
function clamp(value: number, minimum: number, maximum: number): number { return Math.max(minimum, Math.min(maximum, value)); }
function plotRect(): { left: number; right: number; top: number; bottom: number } | null {
  if (!chart || !props.bars.length) return null;
  const bounds = priceBounds();
  const start = clamp(range.value.start, 0, props.bars.length - 1);
  const end = clamp(range.value.end, start, props.bars.length - 1);
  const topLeft = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [start, bounds.max]);
  const bottomRight = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [end, bounds.min]);
  if (!Array.isArray(topLeft) || !Array.isArray(bottomRight)) return null;
  const halfStep = Math.max(1, Math.abs(Number(bottomRight[0]) - Number(topLeft[0])) / Math.max(1, end - start) / 2);
  const top = Math.min(Number(topLeft[1]), Number(bottomRight[1]));
  const bottom = Math.max(Number(topLeft[1]), Number(bottomRight[1]));
  return {
    left: clamp(Number(topLeft[0]) - halfStep, 0, chart.getWidth()),
    right: clamp(Number(bottomRight[0]) + halfStep, 0, chart.getWidth()),
    top: clamp(top, 0, chart.getHeight()),
    bottom: clamp(bottom, 0, chart.getHeight()),
  };
}
function shiftedDrawing(item: ChartDrawing, dx: number, dy: number): ChartDrawing {
  if (!chart) return item;
  const moveX = item.type === "horizontal" ? 0 : dx;
  const moveY = item.type === "vertical" || item.type === "rectangle" ? 0 : dy;
  const points = item.points.map((source) => {
    const pixel = drawingPoint(source);
    if (!pixel) return source;
    const values = chart!.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [pixel[0] + moveX, pixel[1] + moveY]);
    if (!Array.isArray(values)) return source;
    const index = Math.max(0, Math.min(categories.value.length - 1, Math.round(Number(values[0]))));
    return { time: categories.value[index], price: Number(values[1]) };
  });
  return { ...item, points };
}
function dragOptions(item: ChartDrawing): object {
  if (props.drawingsReadOnly || item.style?.locked) return {};
  if (item.type === "rectangle") return { cursor: "ew-resize" };
  return {
    draggable: item.type === "horizontal" ? "vertical" : item.type === "vertical" ? "horizontal" : true,
    cursor: item.type === "horizontal" ? "ns-resize" : item.type === "vertical" ? "ew-resize" : "move",
    ondragend(this: { position?: number[] }) {
      const [dx, dy] = this.position ?? [0, 0];
      emit("updateDrawing", shiftedDrawing(item, Number(dx), Number(dy)));
    },
  };
}
function simplifyBrush(points: ChartDrawingPoint[], tolerance = 1.5): ChartDrawingPoint[] {
  if (points.length < 3) return points;
  const pixels = points.map(drawingPoint);
  const keep = new Set([0, points.length - 1]);
  const simplify = (start: number, end: number): void => {
    const first = pixels[start]; const last = pixels[end];
    if (!first || !last || end - start < 2) return;
    const dx = last[0] - first[0]; const dy = last[1] - first[1]; const length = Math.hypot(dx, dy) || 1;
    let candidate = -1; let maximum = 0;
    for (let index = start + 1; index < end; index += 1) {
      const point = pixels[index]; if (!point) continue;
      const distance = Math.abs(dy * point[0] - dx * point[1] + last[0] * first[1] - last[1] * first[0]) / length;
      if (distance > maximum) { maximum = distance; candidate = index; }
    }
    if (candidate >= 0 && maximum > tolerance) { keep.add(candidate); simplify(start, candidate); simplify(candidate, end); }
  };
  simplify(0, points.length - 1);
  return [...keep].sort((left, right) => left - right).map((index) => points[index]);
}
function shiftedRectanglePoints(points: ChartDrawingPoint[], dx: number): ChartDrawingPoint[] {
  if (!chart || points.length < 2) return points;
  const pixel = drawingPoint(points[0]);
  if (!pixel) return points;
  const values = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [pixel[0] + dx, pixel[1]]);
  if (!Array.isArray(values)) return points;
  const sourceIndexes = points.map((point) => nearestCategoryIndex(point.time));
  if (sourceIndexes.some((index) => index < 0)) return points;
  const sourceIndex = sourceIndexes[0];
  const targetIndex = clamp(Math.round(Number(values[0])), 0, categories.value.length - 1);
  const requestedShift = targetIndex - sourceIndex;
  const minimumShift = -Math.min(...sourceIndexes);
  const maximumShift = categories.value.length - 1 - Math.max(...sourceIndexes);
  const shift = clamp(requestedShift, minimumShift, maximumShift);
  return points.map((point, index) => ({ ...point, time: categories.value[sourceIndexes[index] + shift] }));
}
function resizedRectanglePoints(points: ChartDrawingPoint[], pointIndex: 0 | 1, x: number): ChartDrawingPoint[] {
  if (!chart || points.length < 2) return points;
  const values = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [x, 0]);
  if (!Array.isArray(values)) return points;
  const index = clamp(Math.round(Number(values[0])), 0, categories.value.length - 1);
  return points.map((point, current) => current === pointIndex ? { ...point, time: categories.value[index] } : point);
}
function drawingFromGraphicId(id?: string): ChartDrawing | undefined {
  if (!id?.startsWith("draw_")) return undefined;
  return props.drawings.find((item) => [item.id, `${item.id}_start`, `${item.id}_end`].includes(id.replace(/^draw_/, "")));
}
function renderGraphics(): void {
  chart?.setOption({ graphic: drawingGraphics() }, { replaceMerge: ["graphic"] });
}
function scheduleGraphicsRender(): void {
  if (rectangleRenderFrame != null) return;
  rectangleRenderFrame = requestAnimationFrame(() => {
    rectangleRenderFrame = undefined;
    renderGraphics();
  });
}
function rectangleMetrics(item: ChartDrawing): string {
  return rectangleMetricsForPoints(item.points);
}
function rectangleMetricsForPoints(points: ChartDrawingPoint[]): string {
  const indexes = points.map((point) => nearestCategoryIndex(point.time)).filter((index) => index >= 0);
  if (indexes.length < 2) return "";
  const start = Math.min(...indexes); const end = Math.max(...indexes);
  const bars = props.bars.slice(start, end + 1);
  const highs = bars.map((bar) => asNumber(bar.high)).filter((value): value is number => value != null);
  const lows = bars.map((bar) => asNumber(bar.low)).filter((value): value is number => value != null);
  if (!highs.length || !lows.length) return `K线数量 ${bars.length}`;
  const high = Math.max(...highs); const low = Math.min(...lows);
  const upCount = bars.filter((bar) => (asNumber(bar.close) ?? 0) >= (asNumber(bar.open) ?? 0)).length;
  const downCount = bars.length - upCount;
  const firstBar = bars[0]; const lastBar = bars[bars.length - 1];
  const openPrice = asNumber(firstBar?.open); const closePrice = asNumber(lastBar?.close);
  const pctChange = openPrice && closePrice ? (closePrice - openPrice) / openPrice * 100 : null;
  const amplitude = openPrice ? (high - low) / Math.abs(openPrice) * 100 : null;
  const two = (value: number | null): string => value == null ? "—" : value.toFixed(2);
  return [
    `${bars.length}根K线  ${upCount}根上涨  ${downCount}根下跌  涨幅:${pctChange == null ? "—" : `${pctChange.toFixed(2)}%`}  振幅:${amplitude == null ? "—" : `${amplitude.toFixed(2)}%`}`,
    `开盘:${two(openPrice)}  收盘:${two(closePrice)}  最高:${two(high)}  最低:${two(low)}`,
  ].join("\n");
}
function rectangleBounds(item: ChartDrawing): { high: number; low: number } | null {
  return rectangleBoundsForPoints(item.points);
}
function rectangleBoundsForPoints(points: ChartDrawingPoint[]): { high: number; low: number } | null {
  const indexes = points.map((point) => nearestCategoryIndex(point.time)).filter((index) => index >= 0);
  if (indexes.length < 2) return null;
  const start = Math.min(...indexes); const end = Math.max(...indexes);
  const bars = props.bars.slice(start, end + 1);
  const highs = bars.map((bar) => asNumber(bar.high)).filter((value): value is number => value != null);
  const lows = bars.map((bar) => asNumber(bar.low)).filter((value): value is number => value != null);
  if (!highs.length || !lows.length) return null;
  return { high: Math.max(...highs), low: Math.min(...lows) };
}
function measureTextWidth(text: string): number {
  textMeasureCanvas ??= document.createElement("canvas");
  const context = textMeasureCanvas.getContext("2d");
  if (context) {
    context.font = RECTANGLE_LABEL_FONT;
    return context.measureText(text).width;
  }
  return Array.from(text).reduce((total, character) => total + (character.charCodeAt(0) > 255 ? 10 : 5.5), 0);
}
function readableTextColor(color: string): string {
  const rgba = color.match(/^rgba?\(([^)]+)\)$/i);
  const parts = rgba?.[1].split(/[,/]/).map((part) => Number.parseFloat(part.trim())).filter(Number.isFinite) ?? [];
  const hex = color.replace("#", "");
  const rgb = parts.length >= 3 ? parts.slice(0, 3) : /^[0-9a-f]{6}$/i.test(hex)
    ? [0, 2, 4].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16))
    : /^[0-9a-f]{3}$/i.test(hex)
      ? hex.split("").map((character) => Number.parseInt(`${character}${character}`, 16))
      : [41, 98, 255];
  const [red, green, blue] = rgb;
  const luminance = (Number(red) * 299 + Number(green) * 587 + Number(blue) * 114) / 255000;
  return luminance > 0.68 ? "#17202a" : "#ffffff";
}
function rectangleLayout(points: ChartDrawingPoint[]): {
  first: number[]; second: number[]; x: number; y: number; width: number; height: number;
  labelWidth: number; labelX: number; labelY: number;
} | null {
  const plot = plotRect();
  const first = drawingPoint(points[0]);
  const second = drawingPoint(points[1]);
  if (!plot || !first || !second) return null;
  const x = clamp(Math.min(first[0], second[0]), plot.left, plot.right);
  const endX = clamp(Math.max(first[0], second[0]), plot.left, plot.right);
  const autoBounds = rectangleBoundsForPoints(points);
  let topPix = first[1]; let bottomPix = second[1];
  if (autoBounds && chart) {
    const highPix = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [0, autoBounds.high]);
    const lowPix = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [0, autoBounds.low]);
    if (Array.isArray(highPix) && Array.isArray(lowPix)) {
      topPix = Math.min(Number(highPix[1]), Number(lowPix[1]));
      bottomPix = Math.max(Number(highPix[1]), Number(lowPix[1]));
    }
  }
  const y = clamp(Math.min(topPix, bottomPix), plot.top, plot.bottom);
  const boxBottom = clamp(Math.max(topPix, bottomPix), plot.top, plot.bottom);
  const box = {
    x,
    y,
    width: Math.max(0, endX - x),
    height: Math.max(0, boxBottom - y),
  };
  const plotWidth = Math.max(0, plot.right - plot.left);
  const measuredWidth = Math.max(...rectangleMetricsForPoints(points).split("\n").map(measureTextWidth));
  const labelWidth = Math.min(plotWidth, Math.max(120, measuredWidth + 14));
  const belowSpace = plot.bottom - boxBottom - RECTANGLE_LABEL_GAP;
  const aboveSpace = y - plot.top - RECTANGLE_LABEL_GAP;
  const labelAbove = belowSpace < RECTANGLE_LABEL_HEIGHT && aboveSpace >= belowSpace;
  const requestedLabelY = labelAbove ? y - RECTANGLE_LABEL_HEIGHT - RECTANGLE_LABEL_GAP : boxBottom + RECTANGLE_LABEL_GAP;
  return {
    first,
    second,
    ...box,
    labelWidth,
    labelX: clamp(box.x, plot.left, Math.max(plot.left, plot.right - labelWidth)),
    labelY: clamp(requestedLabelY, plot.top, Math.max(plot.top, plot.bottom - RECTANGLE_LABEL_HEIGHT)),
  };
}
function drawingGraphics(): object[] {
  if (!chart) return [];
  const palette = theme.palette;
  const plot = plotRect();
  if (!plot) return [];
  const graphics = props.drawings.filter((item) => !item.hidden).flatMap<object>((item): object[] => {
    const activeItem = rectangleDrag?.drawingId === item.id ? { ...item, points: rectangleDrag.points } : item;
    const first = activeItem.points[0] && drawingPoint(activeItem.points[0]);
    if (!first) return [];
    const style = activeItem.style ?? {};
    const color = style.color || palette.accent; const width = style.width ?? 1.5; const dash = lineDash(style.lineStyle);
    const selected = props.selectedDrawingId === item.id;
    const selectAnchor = (params: unknown) => {
      const raw = params as { offsetX?: number; offsetY?: number; event?: { offsetX?: number; offsetY?: number } };
      const left = raw.offsetX ?? raw.event?.offsetX;
      const top = raw.offsetY ?? raw.event?.offsetY;
      emit("selectDrawing", item.id, typeof left === "number" && typeof top === "number" ? { left, top } : undefined);
    };
    const common = {
      id: `draw_${item.id}`, silent: props.drawingsReadOnly, z: selected ? 140 : 120,
      onclick: props.drawingsReadOnly ? undefined : selectAnchor,
      ...dragOptions(item),
      style: { stroke: color, lineWidth: selected ? width + 1 : width, lineDash: dash },
    };
    if (item.type === "horizontal") return [
      { type: "line", ...common, shape: { x1: plot.left, y1: clamp(first[1], plot.top, plot.bottom), x2: plot.right, y2: clamp(first[1], plot.top, plot.bottom) } },
      { type: "text", silent: true, z: 141, style: { x: plot.right - 4, y: clamp(first[1] - 8, plot.top, plot.bottom - 16), text: format(activeItem.points[0].price), textAlign: "right", fill: color, backgroundColor: palette.background, padding: [2, 4] } },
    ];
    if (item.type === "vertical") return [
      { type: "line", ...common, shape: { x1: clamp(first[0], plot.left, plot.right), y1: plot.top, x2: clamp(first[0], plot.left, plot.right), y2: plot.bottom } },
      { type: "text", silent: true, z: 141, style: { x: clamp(first[0], plot.left + 36, plot.right - 36), y: plot.bottom - 4, text: timeLabel(activeItem.points[0].time), textAlign: "center", textVerticalAlign: "bottom", fill: color, backgroundColor: palette.background, padding: [2, 4] } },
    ];
    if (item.type === "text") {
      return [{
        type: "text", ...common,
        style: {
          x: clamp(first[0], plot.left, plot.right - 20), y: clamp(first[1], plot.top, plot.bottom - 20), text: activeItem.text || "文本框", fill: style.color || palette.textPrimary,
          font: `bold ${style.fontSize ?? 14}px SimHei, sans-serif`,
        },
        ondblclick: props.drawingsReadOnly ? undefined : () => beginTextEdit(item, first),
      }];
    }
    if (item.type === "brush") {
      const points = activeItem.points.map(drawingPoint).filter((point): point is number[] => point != null);
      return points.length < 2 ? [] : [
        { type: "polyline", ...common, shape: { points }, style: common.style },
        ...(props.drawingsReadOnly ? [] : [{ id: `draw_${item.id}_hit`, type: "polyline", z: 119, shape: { points }, style: { stroke: "rgba(0,0,0,0)", lineWidth: Math.max(6, width + 4) }, onclick: selectAnchor, ...dragOptions(item) }]),
      ];
    }
    const layout = rectangleLayout(activeItem.points);
    if (!layout) return [];
    return [
      { type: "rect", ...common, shape: { x: layout.x, y: layout.y, width: layout.width, height: layout.height }, style: { ...common.style, fill: alpha(style.fillColor || color, style.fillOpacity ?? 0.1) } },
      { type: "text", silent: true, z: 141, style: { x: layout.labelX, y: layout.labelY, text: rectangleMetricsForPoints(activeItem.points), fill: readableTextColor(color), backgroundColor: color, padding: [3, 5], lineHeight: 17, font: RECTANGLE_LABEL_FONT, fontWeight: 400 } },
      ...(selected ? [
        { id: `draw_${item.id}_start`, type: "circle", silent: false, z: 145, shape: { cx: clamp(layout.first[0], plot.left, plot.right), cy: layout.y + layout.height / 2, r: 5 }, style: { fill: "#ffffff", stroke: color, lineWidth: 2 }, onclick: selectAnchor, cursor: "ew-resize" },
        { id: `draw_${item.id}_end`, type: "circle", silent: false, z: 145, shape: { cx: clamp(layout.second[0], plot.left, plot.right), cy: layout.y + layout.height / 2, r: 5 }, style: { fill: "#ffffff", stroke: color, lineWidth: 2 }, onclick: selectAnchor, cursor: "ew-resize" },
      ] : []),
    ];
  });
  if (props.drawingTool === "rectangle" && rectangleCursor.value) {
    const cursorPoint = drawingPoint(rectangleCursor.value);
    if (cursorPoint) {
      const previewColor = DEFAULT_DRAWING_COLOR;
      const previewDot = (pixel: number[], id: string) => ({
        id,
        type: "circle",
        silent: true,
        z: 150,
        shape: { cx: clamp(pixel[0], plot.left, plot.right), cy: clamp(pixel[1], plot.top, plot.bottom), r: 5 },
        style: { fill: "#ffffff", stroke: previewColor, lineWidth: 2 },
      });
      if (!rectangleAnchor.value) {
        graphics.push(previewDot(cursorPoint, "rectangle_preview_cursor"));
      } else {
        const previewLayout = rectangleLayout([rectangleAnchor.value, rectangleCursor.value]);
        const anchorPoint = drawingPoint(rectangleAnchor.value);
        if (previewLayout && anchorPoint) {
          graphics.push({
            id: "rectangle_preview_rect",
            type: "rect",
            silent: true,
            z: 149,
            shape: { x: previewLayout.x, y: previewLayout.y, width: previewLayout.width, height: previewLayout.height },
            style: { fill: alpha(previewColor, 0.08), stroke: previewColor, lineWidth: 1.5, lineDash: [6, 4] },
          });
          graphics.push(previewDot(anchorPoint, "rectangle_preview_anchor"));
          graphics.push(previewDot(cursorPoint, "rectangle_preview_cursor"));
        }
      }
    }
  }
  if (props.drawingTool === "brush" && brushDraft?.points.length) {
    const points = brushDraft.points.map(drawingPoint).filter((point): point is number[] => point != null);
    const style = props.brushStyle ?? {};
    if (points.length > 1) graphics.push({
      id: "brush_preview", type: "polyline", silent: true, z: 150, shape: { points },
      style: { stroke: style.color || DEFAULT_DRAWING_COLOR, lineWidth: style.width ?? 1.5, lineDash: lineDash(style.lineStyle), fill: "none" },
    });
  }
  return graphics;
}

function beginTextEdit(item: ChartDrawing, pixel?: number[]): void {
  const point = item.points[0];
  const anchor = pixel ?? drawingPoint(point);
  if (!point || !anchor) return;
  textDraft.value = { point, left: Number(anchor[0]), top: Number(anchor[1]), value: item.text || "", editingId: item.id };
  void nextTick(() => { textInput.value?.focus(); textInput.value?.select(); });
}

function indicatorSeries(): object[] {
  const palette = theme.palette;
  const line = (name: string, key: string, color: string, yAxisIndex = 0): object | null => {
    const data = props.indicators[key];
    return data?.length ? { name, type: "line", data, yAxisIndex, showSymbol: false, connectNulls: false, lineStyle: { width: 1.4, color }, emphasis: { disabled: true } } : null;
  };
  return [line("MA", "ma", "#f59e0b"), line("HSAR 阻力", "hsarResistance", "#ef4444"), line("HSAR 支撑", "hsarSupport", "#22c55e"), line("布林上轨", "bollingerUpper", "#a855f7"), line("布林中轨", "bollingerMiddle", "#a855f7"), line("布林下轨", "bollingerLower", "#a855f7"), line("ATR 上轨", "atrUpper", "#0ea5e9"), line("ATR 中线", "atrMiddle", "#0ea5e9"), line("ATR 下轨", "atrLower", "#0ea5e9"), line("SD", "sd", palette.chartAxis, 0)].filter((item): item is object => item !== null);
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  if (!props.bars.length) { chart.clear(); return; }
  const palette = theme.palette; const defaultRange = initialRange();
  if (range.value.end >= props.bars.length || range.value.end === 0) range.value = defaultRange;
  const up = props.swapColors ? palette.priceDown : palette.priceUp; const down = props.swapColors ? palette.priceUp : palette.priceDown;
  const compact = props.compact;
  const bounds = priceBounds();
  const quotePanel = displayQuotePanel.value;
  chart.setOption({
    backgroundColor: "transparent", animation: false,
    axisPointer: { show: true, link: [{ xAxisIndex: "all" }], label: { show: !compact, backgroundColor: palette.surfaceSelected, color: palette.textPrimary } },
    tooltip: { trigger: "axis", showContent: false, axisPointer: { type: "cross", snap: true } },
    grid: [
      { left: compact ? 48 : 66, right: compact ? 14 : 42, top: quotePanel ? (compact ? 54 : 58) : (compact ? 26 : 42), height: compact ? (quotePanel ? "43%" : "56%") : quotePanel ? "55%" : "59%" },
      { left: compact ? 48 : 66, right: compact ? 14 : 42, top: compact ? "71%" : "68%", bottom: compact ? 20 : 28 },
    ],
    xAxis: [
      { type: "category", data: categories.value, boundaryGap: true, axisLine: { lineStyle: { color: palette.chartGrid } }, axisLabel: { show: true, interval: (index: number) => isMajorTick(index), hideOverlap: true, color: palette.chartAxis, fontSize: compact ? 8 : 10, formatter: (value: string) => axisTimeLabel(value) }, axisTick: { show: false }, splitLine: { show: true, interval: (index: number) => isMajorTick(index), lineStyle: { color: palette.chartGrid, opacity: 0.8 } }, axisPointer: { show: true, label: { formatter: (params: { value: unknown }) => timeLabel(String(params.value ?? "")) } } },
      { type: "category", gridIndex: 1, data: categories.value, boundaryGap: true, axisLine: { lineStyle: { color: palette.chartGrid } }, axisLabel: { show: true, interval: (index: number) => isMajorTick(index), hideOverlap: true, color: palette.chartAxis, fontSize: compact ? 9 : 11, formatter: (value: string) => axisTimeLabel(value) }, axisTick: { show: false }, splitLine: { show: true, interval: (index: number) => isMajorTick(index), lineStyle: { color: palette.chartGrid, opacity: 0.8 } }, axisPointer: { show: true, label: { formatter: (params: { value: unknown }) => timeLabel(String(params.value ?? "")) } } },
    ],
    yAxis: [
      { scale: true, min: bounds.min, max: bounds.max, inverse: props.inverse, axisLabel: { show: true, color: palette.chartAxis, fontSize: compact ? 8 : 10, showMinLabel: false, showMaxLabel: false }, splitLine: { lineStyle: { color: palette.chartGrid } }, axisPointer: { show: true } },
      { gridIndex: 1, scale: true, splitNumber: 4, axisLabel: { show: true, color: palette.chartAxis, fontSize: compact ? 8 : 10, showMinLabel: false, showMaxLabel: false, formatter: (value: number) => compactAxis(value) }, axisTick: { show: false }, splitLine: { show: true, lineStyle: { color: palette.chartGrid } } },
      { gridIndex: 1, scale: true, splitNumber: 4, position: "right", axisLabel: { show: true, color: palette.chartAxis, fontSize: compact ? 8 : 10, showMinLabel: false, showMaxLabel: false, formatter: (value: number) => secondaryAxis(value) }, axisTick: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], startValue: range.value.start, endValue: range.value.end, zoomOnMouseWheel: false, moveOnMouseWheel: false, moveOnMouseMove: false, preventDefaultMouseMove: true },
    ],
    series: [
      { name: "K线", type: "candlestick", data: candleData.value, itemStyle: { color: up, color0: down, borderColor: up, borderColor0: down }, emphasis: { disabled: true } },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volumeData.value, emphasis: { disabled: true } },
      { name: secondaryMetric.value === "openInterest" ? "持仓量" : "换手率", type: "line", xAxisIndex: 1, yAxisIndex: 2, data: secondaryData.value, showSymbol: false, lineStyle: { width: 1.2, color: palette.accent }, emphasis: { disabled: true } },
      ...indicatorSeries(),
    ],
  }, true);
  renderGraphics();
}

function updateRange(event: unknown): void {
  const batch = (event as { batch?: Array<{ startValue?: number; endValue?: number; start?: number; end?: number }> }).batch?.[0] ?? event as { startValue?: number; endValue?: number; start?: number; end?: number };
  const last = Math.max(0, props.bars.length - 1);
  const start = typeof batch.startValue === "number" ? batch.startValue : typeof batch.start === "number" ? Math.round(batch.start / 100 * last) : range.value.start;
  const end = typeof batch.endValue === "number" ? batch.endValue : typeof batch.end === "number" ? Math.round(batch.end / 100 * last) : range.value.end;
  range.value = { start, end }; emit("visibleRange", start, end);
  if (start <= 15 && !props.loadingEarlier && requestedEarlierForLength !== props.bars.length) {
    requestedEarlierForLength = props.bars.length;
    emit("requestEarlier");
  }
  if (rangeRenderTimer) clearTimeout(rangeRenderTimer);
  rangeRenderTimer = setTimeout(() => {
    rangeRenderTimer = undefined;
    const bounds = priceBounds();
    chart?.setOption({ yAxis: [{ min: bounds.min, max: bounds.max }] });
    renderGraphics();
  }, 80);
}
function coordinateFromEvent(params: { event?: { offsetX?: number; offsetY?: number } }): { time: string; price: number } | null {
  if (!chart || params.event?.offsetX == null || params.event.offsetY == null) return null;
  const cursor = [params.event.offsetX, params.event.offsetY];
  if (!chart.containPixel({ gridIndex: 0 }, cursor)) return null;
  const values = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, cursor);
  if (!Array.isArray(values)) return null;
  const index = Math.max(0, Math.min(props.bars.length - 1, Math.round(Number(values[0]))));
  let price = Number(values[1]);
  if (props.magnet) {
    const candleCenter = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [index, price]);
    const bar = props.bars[index];
    const candidates = [bar.high, bar.low].filter((value): value is number => asNumber(value) != null);
    if (Array.isArray(candleCenter) && Math.abs(Number(candleCenter[0]) - cursor[0]) <= 20 && candidates.length) {
      const nearest = candidates.map((candidate) => ({ candidate, pixel: chart!.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [index, candidate]) })).filter((item) => Array.isArray(item.pixel)).sort((left, right) => Math.abs(Number(left.pixel[1]) - cursor[1]) - Math.abs(Number(right.pixel[1]) - cursor[1]))[0];
      if (nearest && Math.abs(Number(nearest.pixel[1]) - cursor[1]) <= 20) price = nearest.candidate;
    }
  }
  return { time: categories.value[index], price };
}
function chartClick(params: { event?: { offsetX?: number; offsetY?: number } }): void {
  if (props.drawingTool === "cursor") return;
  const point = coordinateFromEvent(params); if (!point) return;
  const anchor = { left: Number(params.event?.offsetX ?? 0), top: Number(params.event?.offsetY ?? 0) };
  if (props.drawingTool === "rectangle") {
    if (!rectangleAnchor.value) {
      rectangleAnchor.value = point;
      rectangleCursor.value = point;
      renderGraphics();
      return;
    }
    emit("draw", { type: "rectangle", points: [rectangleAnchor.value, point] }, anchor);
    rectangleAnchor.value = undefined;
    rectangleCursor.value = undefined;
    renderGraphics();
    return;
  }
  if (props.drawingTool === "text") {
    textDraft.value = { point, left: Number(params.event?.offsetX ?? 0), top: Number(params.event?.offsetY ?? 0), value: "" };
    void nextTick(() => textInput.value?.focus());
    return;
  }
  emit("draw", { type: props.drawingTool, points: [point] }, anchor);
}
function commitText(): void {
  const draft = textDraft.value;
  if (!draft) return;
  const value = draft.value.trim();
  textDraft.value = undefined;
  if (!value) return;
  if (draft.editingId) {
    const item = props.drawings.find((drawing) => drawing.id === draft.editingId);
    if (item) emit("updateDrawing", { ...item, text: value });
  } else emit("draw", { type: "text", points: [draft.point], text: value }, { left: draft.left, top: draft.top });
}
function cancelText(): void { textDraft.value = undefined; }
function captureBrushPointer(payload: unknown): void {
  const raw = payload as { event?: unknown };
  const nested = raw.event as { event?: unknown; pointerId?: unknown; currentTarget?: unknown; target?: unknown } | undefined;
  const native = (nested?.event ?? nested) as { pointerId?: unknown; currentTarget?: unknown; target?: unknown } | undefined;
  const pointerId = Number(native?.pointerId);
  const target = (native?.currentTarget ?? native?.target) as PointerCaptureTarget | undefined;
  if (!Number.isInteger(pointerId) || !target?.setPointerCapture) return;
  try {
    target.setPointerCapture(pointerId);
    brushPointerId = pointerId;
    brushPointerTarget = target;
  } catch {
    // Mouse-only renderer events have no capturable PointerEvent.  ZRender's
    // normal global-out path remains the safe fallback in that case.
  }
}
function releaseBrushPointer(): void {
  if (brushPointerId != null && brushPointerTarget?.releasePointerCapture) {
    try { brushPointerTarget.releasePointerCapture(brushPointerId); } catch { /* capture may already be lost */ }
  }
  brushPointerId = undefined;
  brushPointerTarget = undefined;
}
function pointerDown(event: unknown): void {
  const payload = event as { offsetX?: number; offsetY?: number; target?: { id?: string }; event?: { button?: number; preventDefault?: () => void } };
  if ((payload.event?.button ?? 0) !== 0 || typeof payload.offsetX !== "number") return;
  const item = drawingFromGraphicId(payload.target?.id);
  if (props.drawingTool === "brush") {
    const point = coordinateFromEvent({ event: { offsetX: payload.offsetX, offsetY: payload.offsetY } });
    if (point && typeof payload.offsetY === "number") {
      brushDraft = { points: [point], lastX: payload.offsetX, lastY: payload.offsetY, anchor: { left: payload.offsetX, top: payload.offsetY } };
      captureBrushPointer(payload);
      payload.event?.preventDefault?.();
    }
    return;
  }
  if (props.drawingTool === "cursor" && item?.type === "rectangle" && !props.drawingsReadOnly && !item.style?.locked) {
    const graphicId = payload.target?.id;
    const mode: RectangleDrag["mode"] = graphicId === `draw_${item.id}_start` ? "resize-start" : graphicId === `draw_${item.id}_end` ? "resize-end" : "move";
    const originPoints = item.points.map((point) => ({ ...point }));
    rectangleDrag = { drawingId: item.id, mode, startX: payload.offsetX, originPoints, points: originPoints };
    payload.event?.preventDefault?.();
    return;
  }
  if (props.drawingTool !== "cursor") return;
  if (payload.target?.id?.startsWith("draw_")) return;
  panStartX = payload.offsetX; panOrigin = { ...range.value }; requestedEarlierInGesture = false; payload.event?.preventDefault?.();
}
function pointerMove(event: unknown): void {
  const payload = event as { offsetX?: number; offsetY?: number; event?: { preventDefault?: () => void } };
  const x = payload.offsetX;
  if (brushDraft && props.drawingTool === "brush" && typeof x === "number" && typeof payload.offsetY === "number") {
    const distance = Math.hypot(x - brushDraft.lastX, payload.offsetY - brushDraft.lastY);
    const point = distance >= 2 ? coordinateFromEvent({ event: { offsetX: x, offsetY: payload.offsetY } }) : null;
    if (point) {
      brushDraft.points.push(point); brushDraft.lastX = x; brushDraft.lastY = payload.offsetY;
      if (brushDraft.points.length > 2048) brushDraft.points = brushDraft.points.filter((_item, index) => index % 2 === 0 || index === brushDraft!.points.length - 1);
      scheduleGraphicsRender();
    }
    payload.event?.preventDefault?.();
    return;
  }
  if (rectangleDrag && props.drawingTool === "cursor" && typeof x === "number" && chart) {
    rectangleDrag.points = rectangleDrag.mode === "move"
      ? shiftedRectanglePoints(rectangleDrag.originPoints, x - rectangleDrag.startX)
      : resizedRectanglePoints(rectangleDrag.originPoints, rectangleDrag.mode === "resize-start" ? 0 : 1, x);
    scheduleGraphicsRender();
    payload.event?.preventDefault?.();
    return;
  }
  if (props.drawingTool === "rectangle") {
    if (typeof x !== "number" || typeof payload.offsetY !== "number") {
      if (rectangleCursor.value) {
        rectangleCursor.value = undefined;
        scheduleGraphicsRender();
      }
      return;
    }
    const point = coordinateFromEvent({ event: { offsetX: x, offsetY: payload.offsetY } });
    if (point) {
      rectangleCursor.value = point;
      scheduleGraphicsRender();
    } else if (rectangleCursor.value) {
      rectangleCursor.value = undefined;
      scheduleGraphicsRender();
    }
    payload.event?.preventDefault?.();
    return;
  }
  if (panStartX == null || !panOrigin || typeof x !== "number" || !chart) return;
  const delta = x - panStartX;
  const visible = Math.max(1, panOrigin.end - panOrigin.start + 1);
  const pixelsPerBar = Math.max(2, chart.getWidth() / visible);
  const shift = Math.round(delta / pixelsPerBar);
  if (!requestedEarlierInGesture && panOrigin.start <= 15 && delta >= 24 && !props.loadingEarlier) {
    requestedEarlierInGesture = true;
    requestedEarlierForLength = props.bars.length;
    pendingEarlierShift = Math.max(0, shift - panOrigin.start);
    emit("requestEarlier");
  }
  const maxStart = Math.max(0, props.bars.length - visible);
  const start = Math.max(0, Math.min(maxStart, panOrigin.start - shift));
  const end = Math.min(props.bars.length - 1, start + visible - 1);
  if (start !== range.value.start || end !== range.value.end) {
    range.value = { start, end };
    chart.dispatchAction({ type: "dataZoom", startValue: start, endValue: end });
  }
  payload.event?.preventDefault?.();
}
function pointerUp(): void {
  if (brushDraft) {
    const draft = brushDraft; brushDraft = undefined;
    releaseBrushPointer();
    const points = simplifyBrush(draft.points);
    if (points.length >= 2) emit("draw", { type: "brush", points }, draft.anchor);
    scheduleGraphicsRender();
  }
  if (rectangleDrag) {
    const item = props.drawings.find((drawing) => drawing.id === rectangleDrag?.drawingId);
    const points = rectangleDrag.points;
    rectangleDrag = undefined;
    if (item) emit("updateDrawing", { ...item, points });
    scheduleGraphicsRender();
  }
  panStartX = undefined; panOrigin = undefined; requestedEarlierInGesture = false;
}
function pointerOut(): void {
  if (rectangleCursor.value) {
    rectangleCursor.value = undefined;
    scheduleGraphicsRender();
  }
  pointerUp();
}
function cancelBrushOnEscape(event: KeyboardEvent): void {
  if (event.key !== "Escape" || !brushDraft) return;
  brushDraft = undefined;
  releaseBrushPointer();
  scheduleGraphicsRender();
}
function pointerCancel(event: PointerEvent): void {
  if (!brushDraft || (brushPointerId != null && event.pointerId !== brushPointerId)) return;
  pointerUp();
}
let lastDrawingClickSource: unknown;
function drawingCanvasClick(event: unknown): void {
  const payload = event as { offsetX?: number; offsetY?: number; target?: { id?: string }; source?: unknown };
  if (payload.target?.id?.startsWith("draw_")) return;
  if (props.drawingTool === "cursor") { emit("selectDrawing", ""); return; }
  const source = payload.source ?? payload;
  if (lastDrawingClickSource === source) return;
  lastDrawingClickSource = source;
  chartClick({ event: payload });
}
function seriesCanvasClick(params: unknown): void {
  const raw = params as { offsetX?: number; offsetY?: number; event?: { offsetX?: number; offsetY?: number; target?: { id?: string } } };
  drawingCanvasClick({
    offsetX: raw.offsetX ?? raw.event?.offsetX,
    offsetY: raw.offsetY ?? raw.event?.offsetY,
    target: raw.event?.target,
    source: raw.event ?? params,
  });
}
function wheelZoom(event: unknown): void {
  if (!chart || props.bars.length < 2 || brushDraft) return;
  const payload = event as { offsetX?: number; offsetY?: number; wheelDelta?: number; event?: { wheelDelta?: number; deltaY?: number; preventDefault?: () => void; stopPropagation?: () => void } };
  const cursor = [Number(payload.offsetX ?? 0), Number(payload.offsetY ?? 0)];
  if (!chart.containPixel({ gridIndex: 0 }, cursor) && !chart.containPixel({ gridIndex: 1 }, cursor)) return;
  const wheelDelta = Number(payload.wheelDelta ?? payload.event?.wheelDelta ?? -(payload.event?.deltaY ?? 0));
  if (!wheelDelta) return;
  const currentVisible = Math.max(2, range.value.end - range.value.start + 1);
  const targetVisible = clamp(Math.round(currentVisible * (wheelDelta > 0 ? 0.82 : 1.22)), 8, props.bars.length);
  const converted = chart.convertFromPixel({ xAxisIndex: 0 }, cursor);
  const anchor = Array.isArray(converted) ? clamp(Math.round(Number(converted[0])), 0, props.bars.length - 1) : Math.round((range.value.start + range.value.end) / 2);
  const ratio = currentVisible <= 1 ? 0.5 : clamp((anchor - range.value.start) / (currentVisible - 1), 0, 1);
  let start = Math.round(anchor - ratio * (targetVisible - 1));
  start = clamp(start, 0, Math.max(0, props.bars.length - targetVisible));
  const end = Math.min(props.bars.length - 1, start + targetVisible - 1);
  chart.dispatchAction({ type: "dataZoom", startValue: start, endValue: end });
  payload.event?.preventDefault?.(); payload.event?.stopPropagation?.();
}
function installHandlers(): void {
  if (!chart) return;
  chart.off("updateAxisPointer"); chart.off("datazoom"); chart.off("click");
  chart.on("updateAxisPointer", (event: unknown) => { const value = (event as { axesInfo?: Array<{ value?: number }> }).axesInfo?.[0]?.value; hoverIndex.value = typeof value === "number" ? value : -1; emit("hover", hoverBar.value); });
  chart.on("datazoom", updateRange);
  chart.on("click", seriesCanvasClick);
  const renderer = chart.getZr(); renderer.off("mousedown", pointerDown); renderer.off("mousemove", pointerMove); renderer.off("mouseup", pointerUp); renderer.off("globalout", pointerOut); renderer.off("click", drawingCanvasClick);
  renderer.off("mousewheel", wheelZoom);
  renderer.on("mousedown", pointerDown); renderer.on("mousemove", pointerMove); renderer.on("mouseup", pointerUp); renderer.on("globalout", pointerOut); renderer.on("click", drawingCanvasClick);
  renderer.on("mousewheel", wheelZoom);
}
function resize(): void { chart?.resize(); void nextTick(render); }
watch(() => props.drawingTool, () => {
  rectangleAnchor.value = undefined;
  rectangleCursor.value = undefined;
  rectangleDrag = undefined;
  brushDraft = undefined;
  releaseBrushPointer();
  textDraft.value = undefined;
});
watch(() => props.inverse, () => void nextTick(() => { render(); installHandlers(); }));
watch(() => props.swapColors, () => void nextTick(() => { render(); installHandlers(); }));
watch(() => props.bars, (next, previous) => {
  const previousFirst = previous?.[0] && String(previous[0].barOpenTime || previous[0].tradingDate || "");
  const prepended = previousFirst && next.length > previous.length ? next.findIndex((bar) => String(bar.barOpenTime || bar.tradingDate || "") === previousFirst) : -1;
  if (prepended > 0) {
    const reveal = Math.min(prepended, pendingEarlierShift);
    range.value = { start: Math.max(0, range.value.start + prepended - reveal), end: Math.max(0, range.value.end + prepended - reveal) };
    pendingEarlierShift = 0;
  } else range.value = initialRange();
}, { deep: false });
watch(() => [props.bars, props.indicators, props.inverse, props.swapColors, props.drawings, props.selectedDrawingId, theme.palette], () => void nextTick(() => { render(); installHandlers(); }), { deep: true });
onMounted(() => {
  void nextTick(() => { render(); installHandlers(); });
  window.addEventListener("resize", resize);
  window.addEventListener("keydown", cancelBrushOnEscape);
  element.value?.addEventListener("pointercancel", pointerCancel);
  if (element.value && typeof ResizeObserver !== "undefined") { resizeObserver = new ResizeObserver(resize); resizeObserver.observe(element.value); }
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize);
  window.removeEventListener("keydown", cancelBrushOnEscape);
  element.value?.removeEventListener("pointercancel", pointerCancel);
  releaseBrushPointer();
  resizeObserver?.disconnect();
  if (rangeRenderTimer) clearTimeout(rangeRenderTimer);
  if (rectangleRenderFrame != null) cancelAnimationFrame(rectangleRenderFrame);
  chart?.dispose();
  chart = undefined;
});
</script>

<template>
  <div class="kline-chart chart-box" :class="{ compact, 'drawing-active': drawingTool !== 'cursor' }" :style="{ height: `${height}px` }">
    <div v-if="hoverBar && displayQuotePanel" class="quote-panel" aria-live="polite">
      <time>{{ timeLabel(String(hoverBar.barOpenTime || hoverBar.tradingDate || "")) }}</time>
      <div class="quote-values"><div v-for="(pair, index) in quotePairs" :key="index" class="quote-pair"><p v-for="([name, value], row) in pair" :key="row"><span>{{ name }}</span><strong>{{ value }}</strong></p></div></div>
    </div>
    <div v-else-if="hoverBar" class="quote-strip" aria-live="polite">
      <span>{{ timeLabel(String(hoverBar.barOpenTime || hoverBar.tradingDate || "")) }}</span><span>开 {{ format(hoverBar.open) }}</span><span>高 {{ format(hoverBar.high) }}</span><span>低 {{ format(hoverBar.low) }}</span><span>收 {{ format(hoverBar.close) }}</span><span>成交量 {{ futureUnits ? futureUnit(hoverBar.volume) : stockUnit(hoverBar.volume) }}</span><span v-if="secondaryMetric === 'turnoverRate'">换手率 {{ format(hoverBar.turnoverRate) }}%</span><span v-else>持仓量 {{ futureUnits ? futureUnit(hoverBar.openInterest) : format(hoverBar.openInterest, 0) }}</span><template v-if="!compact"><span>成交额 {{ futureUnits ? futureUnit(hoverBar.amount) : stockUnit(hoverBar.amount) }}</span><span>结算价 {{ format(hoverBar.settlement) }}</span><span>涨跌 {{ format(hoverBar.change) }}</span><span>涨幅 {{ format(hoverBar.pctChange) }}%</span><span>振幅 {{ format(hoverBar.amplitude) }}%</span><span>沉淀资金 {{ futureUnits ? futureUnit(hoverBar.capitalDeposit) : stockUnit(hoverBar.capitalDeposit) }}</span></template>
    </div>
    <div
      ref="element"
      class="chart-root"
      :data-rectangle-preview="Boolean(rectangleCursor)"
      :data-rectangle-anchor="Boolean(rectangleAnchor)"
      :data-drawing-count="drawings.filter((item) => !item.hidden).length"
    />
    <span class="secondary-axis-name secondary-axis-name-left">成交量</span>
    <span class="secondary-axis-name secondary-axis-name-right">{{ secondaryMetricLabel }}</span>
    <input
      v-if="textDraft"
      ref="textInput"
      v-model="textDraft.value"
      class="chart-text-input"
      :style="{ left: `${textDraft.left}px`, top: `${textDraft.top}px` }"
      aria-label="图表文字"
      placeholder="输入文字"
      @keydown.enter.prevent="commitText"
      @keydown.esc.prevent="cancelText"
      @blur="commitText"
    />
    <div v-if="loadingEarlier" class="history-loading">正在加载更早的 K 线…</div>
    <div v-if="bars.length === 0" class="chart-empty">暂无 K 线数据</div>
  </div>
</template>

<style scoped>
.kline-chart { position: relative; width: 100%; min-width: 0; user-select: none; }
.chart-root { width: 100%; height: 100%; }
.quote-strip { position: absolute; z-index: 2; top: 4px; left: 72px; right: 42px; display: flex; flex-wrap: wrap; gap: 3px 10px; max-height: 34px; overflow: hidden; color: var(--ml-text-secondary); font: 11px/1.4 ui-monospace, Consolas, monospace; pointer-events: none; }
.quote-strip span:nth-child(5) { color: var(--ml-text-primary); font-weight: 700; }
.compact .quote-strip { left: 50px; right: 14px; gap: 2px 6px; max-height: 18px; font-size: 9px; white-space: nowrap; }
.quote-panel { position:absolute; z-index:2; top:2px; left:66px; right:42px; height:52px; pointer-events:none; overflow:hidden; font-family:"SimHei","Heiti SC","Microsoft YaHei",sans-serif; }
.quote-panel time { display:block; height:16px; color:var(--ml-text-primary); font-size:11px; font-weight:900; line-height:16px; white-space:nowrap; }
.quote-values { display:grid; grid-template-columns:repeat(7,minmax(86px,1fr)); height:34px; }
.quote-pair { display:grid; grid-template-rows:1fr 1fr; padding:0 6px; border-left:1px solid var(--ml-divider); }
.quote-pair p { display:grid; grid-template-columns:auto 1fr; align-items:center; gap:7px; margin:0; min-width:0; }
.quote-pair span { color:var(--ml-text-secondary); font-size:11px; white-space:nowrap; font-weight:bold; }
.quote-pair strong { overflow:hidden; color:var(--ml-text-primary); font-size:13px; font-weight:900; font-family:"SimHei","Heiti SC",sans-serif; text-overflow:ellipsis; white-space:nowrap; }
.compact .quote-panel { left:48px; right:14px; height:50px; }
.compact .quote-panel time { height:14px; font-size:9px; line-height:14px; }
.compact .quote-values { height:34px; grid-template-columns:repeat(7,minmax(0,1fr)); overflow:hidden; }
.compact .quote-pair { overflow:hidden; padding:0 2px; }
.compact .quote-pair p { gap:1px; }
.compact .quote-pair span { font-size:8px; }
.compact .quote-pair strong { font-size:9px; }
.secondary-axis-name { position:absolute; z-index:2; bottom:2px; color:var(--ml-text-secondary); font:700 10px/1 "Microsoft YaHei UI","Microsoft YaHei",sans-serif; pointer-events:none; }
.secondary-axis-name-left { left:66px; }
.secondary-axis-name-right { right:42px; }
.compact .secondary-axis-name { font-size:8px; }
.compact .secondary-axis-name-left { left:48px; }
.compact .secondary-axis-name-right { right:14px; }
.history-loading { position:absolute; z-index:5; top:8px; right:8px; padding:4px 8px; border:1px solid var(--ml-divider); border-radius:6px; background:color-mix(in srgb,var(--ml-surface) 90%,transparent); color:var(--ml-text-secondary); font-size:11px; pointer-events:none; }
.chart-text-input { position:absolute; z-index:180; min-width:150px; max-width:280px; transform:translateY(-50%); padding:5px 7px; border:1px solid var(--ml-accent); border-radius:3px; outline:2px solid color-mix(in srgb,var(--ml-accent) 22%,transparent); background:var(--ml-surface); color:var(--ml-text-primary); font:14px/1.4 "Microsoft YaHei UI","Microsoft YaHei",sans-serif; }
.drawing-active :deep(canvas) { cursor:crosshair !important; }
.chart-empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--ml-text-secondary); font-size: 13px; }
</style>
