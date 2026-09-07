<script setup lang="ts">
import * as echarts from "echarts";
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import type { IndicatorInstance, VolumeProfile } from "../../domain/strategyTypes";
import { useThemeStore } from "../../stores/theme";
import { heikinAshi, type ChartType } from "../../domain/chartPresentation";
import { CHART_LAYOUT, nestedBarGeometry } from "../../domain/chartLayout";
import { weekdayLabel } from "../../domain/marketList";
import LaserCanvas from "./LaserCanvas.vue";
import QuoteValues from "./QuoteValues.vue";

export interface KLineBar {
  barOpenTime?: string;
  tradingDate?: string;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  volume?: number;
  amount?: number;
  turnoverRate?: number;
  openInterest?: number;
  settlement?: number;
  change?: number;
  pctChange?: number;
  amplitude?: number;
  capitalDeposit?: number;
  capitalDepositReason?: string;
}
export type DrawingTool =
  | "cursor"
  | "horizontal"
  | "vertical"
  | "trend"
  | "long_position"
  | "short_position"
  | "rectangle"
  | "fibonacci_retracement"
  | "text"
  | "laser"
  | "brush";
export type DrawingLineStyle = "solid" | "dashed" | "dotted" | "dashdot";
export interface ChartDrawingStyle {
  color?: string;
  width?: number;
  lineStyle?: DrawingLineStyle;
  fillColor?: string;
  fillOpacity?: number;
  fontSize?: number;
  borderColor?: string;
  borderWidth?: number;
  borderStyle?: DrawingLineStyle;
  locked?: boolean;
}
export interface ChartDrawingPoint {
  time: string;
  price: number;
}
export interface FibonacciRetracementLevel {
  ratio: number;
  label: string;
}
const DEFAULT_FIBONACCI_RETRACEMENT_LEVELS: readonly FibonacciRetracementLevel[] = [
  { ratio: 0, label: "0.0%" },
  { ratio: 0.236, label: "23.6%" },
  { ratio: 0.382, label: "38.2%" },
  { ratio: 0.5, label: "50.0%" },
  { ratio: 0.618, label: "61.8%" },
  { ratio: 0.786, label: "78.6%" },
  { ratio: 1, label: "100.0%" },
];
export interface ChartDrawing {
  id: string;
  type: Exclude<DrawingTool, "cursor" | "laser">;
  version?: number;
  period?: string;
  crossPeriod?: boolean;
  hidden?: boolean;
  text?: string;
  points: ChartDrawingPoint[];
  levels?: FibonacciRetracementLevel[];
  style?: ChartDrawingStyle;
}
export interface StrategyChartMarker {
  kind: "entry" | "exit";
  label?: string;
  time: string;
  price: number;
  barIndex: number;
  reason: string;
  tradeId?: string;
}
const DEFAULT_DRAWING_COLOR = "#2196f3";
const RECTANGLE_LABEL_FONT =
  '12px "Microsoft YaHei UI","Microsoft YaHei",sans-serif';
const RECTANGLE_LABEL_HEIGHT = 42;
const RECTANGLE_LABEL_GAP = 4;
interface DrawingAnchor {
  left: number;
  top: number;
}
interface RectangleDrag {
  drawingId: string;
  mode: "move" | "resize-start" | "resize-end";
  startX: number;
  originPoints: ChartDrawingPoint[];
  points: ChartDrawingPoint[];
}
interface FibonacciDrag {
  drawingId: string;
  pointIndex: 0 | 1;
  points: ChartDrawingPoint[];
}
interface BrushDraft {
  points: ChartDrawingPoint[];
  lastX: number;
  lastY: number;
  anchor: DrawingAnchor;
}
interface PointerCaptureTarget extends EventTarget {
  setPointerCapture?: (pointerId: number) => void;
  releasePointerCapture?: (pointerId: number) => void;
}

const props = withDefaults(
  defineProps<{
    bars: KLineBar[];
    height?: number;
    fill?: boolean;
    indicators?: Record<string, Array<number | null | undefined>>;
    indicatorInstances?: IndicatorInstance[];
    volumeProfile?: VolumeProfile;
    strategyMarkers?: StrategyChartMarker[];
    inverse?: boolean;
    swapColors?: boolean;
    drawings?: ChartDrawing[];
    drawingTool?: DrawingTool;
    magnet?: boolean;
    defaultVisible?: number;
    compact?: boolean;
    selectedDrawingId?: string;
    showQuotePanel?: boolean;
    totalMarketCap?: number;
    floatMarketCap?: number;
    loadingEarlier?: boolean;
    period?: string;
    drawingsReadOnly?: boolean;
    futureUnits?: boolean;
    brushStyle?: ChartDrawingStyle;
    chartType?: ChartType;
    hideQuote?: boolean;
    replayPick?: boolean;
    openOnDoubleClick?: boolean;
  }>(),
  {
    bars: () => [],
    height: 460,
    fill: false,
    indicators: () => ({}),
    indicatorInstances: () => [],
    volumeProfile: undefined,
    strategyMarkers: () => [],
    inverse: false,
    swapColors: false,
    drawings: () => [],
    drawingTool: "cursor",
    magnet: false,
    defaultVisible: 60,
    compact: false,
    selectedDrawingId: "",
    showQuotePanel: false,
    totalMarketCap: undefined,
    floatMarketCap: undefined,
    loadingEarlier: false,
    period: "1d",
    drawingsReadOnly: false,
    futureUnits: false,
    brushStyle: () => ({}),
    chartType: "candles",
    hideQuote: false,
    openOnDoubleClick: false,
  },
);
const emit = defineEmits<{
  hover: [bar: KLineBar | null];
  pickBar: [index: number];
  draw: [drawing: Omit<ChartDrawing, "id">, anchor?: DrawingAnchor];
  visibleRange: [start: number, end: number];
  selectDrawing: [id: string, anchor?: DrawingAnchor];
  updateDrawing: [drawing: ChartDrawing];
  requestEarlier: [];
  openDetail: [];
}>();
const theme = useThemeStore();
const element = ref<HTMLElement>();
const layoutHeight = ref(props.height);
const hoverIndex = ref(-1);
const range = ref({ start: 0, end: 0 });
let chart: echarts.ECharts | undefined;
const riskPoints = ref<ChartDrawingPoint[]>([]);
const riskHint = ref("");
const rectangleAnchor = ref<ChartDrawingPoint>();
const rectangleCursor = ref<ChartDrawingPoint>();
const fibonacciAnchor = ref<ChartDrawingPoint>();
const fibonacciCursor = ref<ChartDrawingPoint>();
const textDraft = ref<{
  point: { time: string; price: number };
  left: number;
  top: number;
  value: string;
  editingId?: string;
}>();
const textInput = ref<HTMLInputElement>();
let panStartX: number | undefined;
let panOrigin: { start: number; end: number } | undefined;
let rectangleDrag: RectangleDrag | undefined;
let fibonacciDrag: FibonacciDrag | undefined;
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
let hoverFrame: number | undefined;
let pendingHoverIndex = -1;

const categories = computed(() =>
  props.bars.map((bar) => String(bar.barOpenTime || bar.tradingDate || "")),
);
const hoverBar = computed(
  () => props.bars[hoverIndex.value] ?? props.bars.at(-1) ?? null,
);
const candleData = computed(() =>
  props.bars.map((bar) => [
    asNumber(bar.open),
    asNumber(bar.close),
    asNumber(bar.low),
    asNumber(bar.high),
  ]),
);
const volumeData = computed(() =>
  props.bars.map((bar) => ({
    value: asNumber(bar.volume),
    itemStyle: { color: upColor(bar) },
  })),
);
const secondaryMetric = computed<"openInterest" | "amount">(() =>
  props.bars.some((bar) => asNumber(bar.openInterest) != null)
    ? "openInterest"
    : "amount",
);
const secondaryData = computed(() =>
  props.bars.map((bar) => ({
    value: asNumber(bar[secondaryMetric.value]),
    // Both bars retain the candle's up/down semantics.  The secondary measure is
    // deliberately lighter and is painted first, so volume remains readable.
    itemStyle: { color: upColor(bar), opacity: 0.34 },
  })),
);

function nestedBarSeries(
  name: string,
  data: Array<{ value: number | null; itemStyle: { color: string; opacity?: number } }>,
  yAxisIndex: number,
  widthRatio: number,
  z: number,
): object {
  return {
    name,
    type: "custom",
    xAxisIndex: 1,
    yAxisIndex,
    coordinateSystem: "cartesian2d",
    encode: { x: 0, y: 1 },
    data: data.map((item, index) => [index, item.value, item.itemStyle.color, item.itemStyle.opacity ?? 1]),
    silent: true,
    z,
    emphasis: { disabled: true },
    renderItem(params: { coordSys?: { x: number; y: number; width: number; height: number } }, api: {
      value(dimension: number): unknown;
      coord(value: [number, number]): [number, number];
      size(value: [number, number]): [number, number];
    }) {
      const index = Number(api.value(0));
      const value = Number(api.value(1));
      if (!Number.isFinite(index) || !Number.isFinite(value)) return undefined;
      const center = api.coord([index, 0])[0];
      const baseline = api.coord([index, 0])[1];
      const valueY = api.coord([index, value])[1];
      const width = Math.max(0, api.size([1, 0])[0]);
      const geometry = nestedBarGeometry(center, width);
      const selected = widthRatio >= CHART_LAYOUT.secondaryBackRatio ? geometry.back : geometry.front;
      const shape = {
        x: selected.x,
        y: Math.min(baseline, valueY),
        width: selected.width,
        height: Math.abs(baseline - valueY),
      };
      const clipped = params.coordSys
        ? echarts.graphic.clipRectByRect(shape, params.coordSys)
        : shape;
      if (!clipped) return undefined;
      return {
        type: "rect",
        shape: clipped,
        style: { fill: String(api.value(2)), opacity: Number(api.value(3)) },
      };
    },
  };
}
const majorTicks = computed(() => {
  const indexes = new Set<number>();
  let previous = "";
  categories.value.forEach((value, index) => {
    const day = value.slice(0, 10);
    const month = day.slice(0, 7);
    const year = day.slice(0, 4);
    const key = ["5m", "15m", "30m", "1h", "2h"].includes(props.period)
      ? day
      : ["1d", "1w"].includes(props.period)
        ? month
        : year;
    if (index === 0 || key !== previous) indexes.add(index);
    previous = key;
  });
  return indexes;
});
const quotePairs = computed(() => {
  const bar = hoverBar.value;
  const volumeText = props.futureUnits
    ? futureUnit(bar?.volume)
    : stockUnit(bar?.volume);
  const amountText = props.futureUnits
    ? futureUnit(bar?.amount)
    : stockUnit(bar?.amount);
  const openInterestText = props.futureUnits
    ? futureUnit(bar?.openInterest)
    : format(bar?.openInterest, 0);
  const depositText = props.futureUnits
    ? futureUnit(bar?.capitalDeposit)
    : stockUnit(bar?.capitalDeposit);
  return [
    [
      ["开盘价", format(bar?.open)],
      ["收盘价", format(bar?.close)],
    ],
    [
      ["最高价", format(bar?.high)],
      ["最低价", format(bar?.low)],
    ],
    [
      ["结算价", format(bar?.settlement)],
      ["振幅", percent(bar?.amplitude)],
    ],
    [
      ["涨幅", percent(bar?.pctChange)],
      ["涨跌", format(bar?.change)],
    ],
    [
      ["成交量", volumeText],
      ["成交额", amountText],
    ],
    [
      ["持仓量", openInterestText],
      ["沉淀资金", depositText],
    ],
    [
      ["总市值", format(props.totalMarketCap, 2)],
      ["流通市值", format(props.floatMarketCap, 2)],
    ],
  ];
});
const displayQuotePanel = computed(
  () => props.showQuotePanel || !props.compact,
);
const secondaryMetricLabel = computed(() =>
  secondaryMetric.value === "openInterest" ? "持仓量" : "成交额",
);
const paneIndicatorInstances = computed(() =>
  props.indicatorInstances.filter(
    (item) =>
      item.visible && item.status === "ready" && item.placement === "pane",
  ),
);

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function format(value: unknown, digits = 4): string {
  const item = asNumber(value);
  return item == null
    ? "—"
    : item.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}
function percent(value: unknown): string {
  const item = asNumber(value);
  return item == null ? "—" : `${item.toFixed(2)}%`;
}
function stockUnit(value: unknown): string {
  const item = asNumber(value);
  return item == null ? "—" : `${(item / 1e8).toFixed(2)}亿`;
}
function futureUnit(value: unknown): string {
  const item = asNumber(value);
  return item == null ? "—" : `${(item / 1e4).toFixed(2)}万`;
}
function compactAxis(value: unknown): string {
  const item = asNumber(value);
  if (item == null) return "—";
  const abs = Math.abs(item);
  if (abs >= 1e8) return `${(item / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(item / 1e4).toFixed(2)}万`;
  return item.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function timeLabel(value: string): string {
  const text = value.replace("T", " ");
  const day = text.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return text.slice(0, 16);
  const weekday = weekdayLabel(day);
  return ["5m", "15m", "30m", "1h", "2h"].includes(props.period)
    ? `${day} ${weekday} ${text.slice(11, 16)}`.trim()
    : `${day} ${weekday}`;
}
function axisTimeLabel(value: string): string {
  const day = value.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return timeLabel(value);
  const weekday = weekdayLabel(day);
  return ["5m", "15m", "30m", "1h", "2h"].includes(props.period) ? timeLabel(value).slice(5) : `${day.slice(5)} ${weekday}`;
}
function isMajorTick(index: number): boolean {
  return majorTicks.value.has(index);
}
function upColor(bar: KLineBar): string {
  const up = (asNumber(bar.close) ?? 0) >= (asNumber(bar.open) ?? 0);
  const palette = theme.palette;
  return props.swapColors
    ? up
      ? palette.priceDown
      : palette.priceUp
    : up
      ? palette.priceUp
      : palette.priceDown;
}
function initialRange(): { start: number; end: number } {
  const total = props.bars.length;
  return {
    start: Math.max(0, total - Math.min(props.defaultVisible, total)),
    end: Math.max(0, total - 1),
  };
}
function priceBounds(): { min: number; max: number; span: number } {
  const windowBars = (props.chartType === "heikin" ? heikinAshi(props.bars) : props.bars).slice(
    Math.max(0, range.value.start),
    Math.min(props.bars.length, range.value.end + 1),
  );
  const values = windowBars
    .flatMap((bar) => [asNumber(bar.high), asNumber(bar.low)])
    .filter((value): value is number => value != null);
  const rawMin = values.length ? Math.min(...values) : 0;
  const rawMax = values.length ? Math.max(...values) : 1;
  const span = Math.max(
    Math.abs(rawMax - rawMin),
    Math.abs(rawMax || 1) * 0.01,
  );
  const padding = props.compact ? 0.08 : 0.18;
  return { min: rawMin - span * padding, max: rawMax + span * padding, span };
}
function lineDash(value?: DrawingLineStyle): number[] | undefined {
  if (value === "dashed") return [8, 4];
  if (value === "dotted") return [2, 3];
  if (value === "dashdot") return [10, 5, 2, 5];
  return undefined;
}
function fibonacciLevels(item: ChartDrawing): FibonacciRetracementLevel[] {
  const levels = item.levels?.filter(
    (level) =>
      Number.isFinite(level.ratio) &&
      typeof level.label === "string" &&
      Boolean(level.label.trim()),
  );
  return levels?.length
    ? levels
    : DEFAULT_FIBONACCI_RETRACEMENT_LEVELS.map((level) => ({ ...level }));
}
function fibonacciRetracementPrice(
  start: number,
  end: number,
  ratio: number,
): number {
  return end + (start - end) * ratio;
}
function alpha(color: string, opacity: number): string {
  const value = color.replace("#", "");
  if (/^[0-9a-f]{6}$/i.test(value)) {
    const [red, green, blue] = [0, 2, 4].map((offset) =>
      Number.parseInt(value.slice(offset, offset + 2), 16),
    );
    return `rgba(${red},${green},${blue},${opacity})`;
  }
  return color;
}
function drawingPoint(item: { time: string; price: number }): number[] | null {
  if (!chart) return null;
  const index = nearestCategoryIndex(item.time);
  if (index < 0) return null;
  const pixel = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
    index,
    item.price,
  ]);
  if (!Array.isArray(pixel)) return null;
  // Interpolate between logical bar times. Category axes round coordinates,
  // which otherwise quantizes every freehand point into a vertical stair step.
  const target = Date.parse(item.time);
  const centerTime = Date.parse(categories.value[index]);
  const neighbor = target < centerTime ? index - 1 : index + 1;
  if (neighbor >= 0 && neighbor < categories.value.length && target !== centerTime) {
    const otherTime = Date.parse(categories.value[neighbor]);
    const other = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [neighbor, item.price]);
    if (Array.isArray(other) && Number.isFinite(target) && otherTime !== centerTime) {
      pixel[0] = Number(pixel[0]) + (Number(other[0]) - Number(pixel[0])) * (target - centerTime) / (otherTime - centerTime);
    }
  }
  return pixel.map(Number);
}
function secondaryAxis(value: unknown): string {
  return compactAxis(value);
}
function requestDetail(event: MouseEvent): void {
  if (!props.bars.length || !props.openOnDoubleClick || event.defaultPrevented) return;
  if ((event.target as HTMLElement | null)?.closest("input, button, select, textarea")) return;
  emit("openDetail");
}
function nearestCategoryIndex(time: string): number {
  const exact = categories.value.indexOf(time);
  if (exact >= 0) return exact;
  const target = Date.parse(time);
  if (!Number.isFinite(target)) return -1;
  return categories.value.reduce((nearest, value, candidate) => {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return nearest;
    if (nearest < 0) return candidate;
    return Math.abs(timestamp - target) <
      Math.abs(Date.parse(categories.value[nearest]) - target)
      ? candidate
      : nearest;
  }, -1);
}
function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}
function plotRect(): {
  left: number;
  right: number;
  top: number;
  bottom: number;
} | null {
  if (!chart || !props.bars.length) return null;
  const bounds = priceBounds();
  const start = clamp(range.value.start, 0, props.bars.length - 1);
  const end = clamp(range.value.end, start, props.bars.length - 1);
  const topLeft = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
    start,
    bounds.max,
  ]);
  const bottomRight = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
    end,
    bounds.min,
  ]);
  if (!Array.isArray(topLeft) || !Array.isArray(bottomRight)) return null;
  const halfStep = Math.max(
    1,
    Math.abs(Number(bottomRight[0]) - Number(topLeft[0])) /
      Math.max(1, end - start) /
      2,
  );
  const top = Math.min(Number(topLeft[1]), Number(bottomRight[1]));
  const bottom = Math.max(Number(topLeft[1]), Number(bottomRight[1]));
  return {
    left: clamp(Number(topLeft[0]) - halfStep, 0, chart.getWidth()),
    right: clamp(Number(bottomRight[0]) + halfStep, 0, chart.getWidth()),
    top: clamp(top, 0, chart.getHeight()),
    bottom: clamp(bottom, 0, chart.getHeight()),
  };
}
function shiftedDrawing(
  item: ChartDrawing,
  dx: number,
  dy: number,
): ChartDrawing {
  if (!chart) return item;
  const moveX = item.type === "horizontal" ? 0 : dx;
  const moveY = item.type === "vertical" || item.type === "rectangle" ? 0 : dy;
  const points = item.points.map((source) => {
    const pixel = drawingPoint(source);
    if (!pixel) return source;
    if (item.type === "brush") {
      return coordinateFromEvent({ event: { offsetX: pixel[0] + moveX, offsetY: pixel[1] + moveY } }, true) || source;
    }
    const values = chart!.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
      pixel[0] + moveX,
      pixel[1] + moveY,
    ]);
    if (!Array.isArray(values)) return source;
    const index = Math.max(
      0,
      Math.min(categories.value.length - 1, Math.round(Number(values[0]))),
    );
    return { time: categories.value[index], price: Number(values[1]) };
  });
  return { ...item, points };
}
function dragOptions(item: ChartDrawing): object {
  if (props.drawingsReadOnly || item.style?.locked) return {};
  if (item.type === "rectangle") return { cursor: "ew-resize" };
  return {
    draggable:
      item.type === "horizontal"
        ? "vertical"
        : item.type === "vertical"
          ? "horizontal"
          : true,
    cursor:
      item.type === "horizontal"
        ? "ns-resize"
        : item.type === "vertical"
          ? "ew-resize"
          : "move",
    ondragend(this: { position?: number[] }) {
      const [dx, dy] = this.position ?? [0, 0];
      emit("updateDrawing", shiftedDrawing(item, Number(dx), Number(dy)));
    },
  };
}
function simplifyBrush(
  points: ChartDrawingPoint[],
  tolerance = 1.5,
): ChartDrawingPoint[] {
  if (points.length < 3) return points;
  const pixels = points.map(drawingPoint);
  const keep = new Set([0, points.length - 1]);
  const simplify = (start: number, end: number): void => {
    const first = pixels[start];
    const last = pixels[end];
    if (!first || !last || end - start < 2) return;
    const dx = last[0] - first[0];
    const dy = last[1] - first[1];
    const length = Math.hypot(dx, dy) || 1;
    let candidate = -1;
    let maximum = 0;
    for (let index = start + 1; index < end; index += 1) {
      const point = pixels[index];
      if (!point) continue;
      const distance =
        Math.abs(
          dy * point[0] -
            dx * point[1] +
            last[0] * first[1] -
            last[1] * first[0],
        ) / length;
      if (distance > maximum) {
        maximum = distance;
        candidate = index;
      }
    }
    if (candidate >= 0 && maximum > tolerance) {
      keep.add(candidate);
      simplify(start, candidate);
      simplify(candidate, end);
    }
  };
  simplify(0, points.length - 1);
  return [...keep]
    .sort((left, right) => left - right)
    .map((index) => points[index]);
}
function shiftedRectanglePoints(
  points: ChartDrawingPoint[],
  dx: number,
): ChartDrawingPoint[] {
  if (!chart || points.length < 2) return points;
  const pixel = drawingPoint(points[0]);
  if (!pixel) return points;
  const values = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
    pixel[0] + dx,
    pixel[1],
  ]);
  if (!Array.isArray(values)) return points;
  const sourceIndexes = points.map((point) => nearestCategoryIndex(point.time));
  if (sourceIndexes.some((index) => index < 0)) return points;
  const sourceIndex = sourceIndexes[0];
  const targetIndex = clamp(
    Math.round(Number(values[0])),
    0,
    categories.value.length - 1,
  );
  const requestedShift = targetIndex - sourceIndex;
  const minimumShift = -Math.min(...sourceIndexes);
  const maximumShift = categories.value.length - 1 - Math.max(...sourceIndexes);
  const shift = clamp(requestedShift, minimumShift, maximumShift);
  return points.map((point, index) => ({
    ...point,
    time: categories.value[sourceIndexes[index] + shift],
  }));
}
function resizedRectanglePoints(
  points: ChartDrawingPoint[],
  pointIndex: 0 | 1,
  x: number,
): ChartDrawingPoint[] {
  if (!chart || points.length < 2) return points;
  const values = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
    x,
    0,
  ]);
  if (!Array.isArray(values)) return points;
  const index = clamp(
    Math.round(Number(values[0])),
    0,
    categories.value.length - 1,
  );
  return points.map((point, current) =>
    current === pointIndex
      ? { ...point, time: categories.value[index] }
      : point,
  );
}
function resizedFibonacciPoints(
  points: ChartDrawingPoint[],
  pointIndex: 0 | 1,
  x: number,
  y: number,
): ChartDrawingPoint[] {
  if (!chart || points.length < 2) return points;
  const values = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
    x,
    y,
  ]);
  if (!Array.isArray(values)) return points;
  const index = clamp(
    Math.round(Number(values[0])),
    0,
    categories.value.length - 1,
  );
  const price = Number(values[1]);
  if (!Number.isFinite(price)) return points;
  return points.map((point, current) =>
    current === pointIndex
      ? { time: categories.value[index], price }
      : point,
  );
}
function drawingFromGraphicId(id?: unknown): ChartDrawing | undefined {
  if (typeof id !== "string" || !id.startsWith("draw_")) return undefined;
  return props.drawings.find((item) =>
    [item.id, `${item.id}_start`, `${item.id}_end`].includes(
      id.replace(/^draw_/, ""),
    ),
  );
}
function renderGraphics(): void {
  chart?.setOption(
    { graphic: [...volumeProfileGraphics(), ...drawingGraphics()] },
    { replaceMerge: ["graphic"] },
  );
}
function scheduleGraphicsRender(): void {
  if (rectangleRenderFrame != null) return;
  rectangleRenderFrame = requestAnimationFrame(() => {
    rectangleRenderFrame = undefined;
    renderGraphics();
  });
}
function volumeProfileGraphics(): object[] {
  const profile = props.volumeProfile;
  if (
    !chart ||
    !profile ||
    !profile.buckets.length ||
    profile.range.startIndex !== range.value.start ||
    profile.range.endIndex !== range.value.end ||
    profile.totalVolume <= 0
  )
    return [];
  const plot = plotRect();
  if (!plot) return [];
  const maximum = Math.max(...profile.buckets.map((item) => item.volume));
  if (maximum <= 0) return [];
  const profileWidth = Math.max(
    28,
    Math.min(140, (plot.right - plot.left) * 0.22),
  );
  const palette = theme.palette;
  const graphics: object[] = [];
  for (const [index, bucket] of profile.buckets.entries()) {
    if (bucket.volume <= 0) continue;
    const low = chart.convertToPixel(
      { xAxisIndex: 0, yAxisIndex: 0 },
      [range.value.end, bucket.bucketLow],
    );
    const high = chart.convertToPixel(
      { xAxisIndex: 0, yAxisIndex: 0 },
      [range.value.end, bucket.bucketHigh],
    );
    if (!Array.isArray(low) || !Array.isArray(high)) continue;
    const top = clamp(
      Math.min(Number(low[1]), Number(high[1])),
      plot.top,
      plot.bottom,
    );
    const bottom = clamp(
      Math.max(Number(low[1]), Number(high[1])),
      plot.top,
      plot.bottom,
    );
    const width = Math.max(2, profileWidth * (bucket.volume / maximum));
    const color = bucket.isPoc
      ? palette.accent
      : bucket.isValueArea
        ? palette.priceUp
        : palette.chartAxis;
    graphics.push({
      id: `volume_profile_bucket_${index}`,
      type: "rect",
      silent: true,
      z: 40,
      shape: {
        x: plot.right - width,
        y: top,
        width,
        height: Math.max(1, bottom - top),
      },
      style: {
        fill: alpha(
          color,
          bucket.isPoc ? 0.72 : bucket.isValueArea ? 0.42 : 0.24,
        ),
      },
    });
    if (bucket.isPoc)
      graphics.push({
        id: "volume_profile_poc_label",
        type: "text",
        silent: true,
        z: 41,
        style: {
          x: plot.right - width - 3,
          y: (top + bottom) / 2,
          text: "POC",
          textAlign: "right",
          textVerticalAlign: "middle",
          fill: palette.accent,
          font: '10px "Microsoft YaHei UI", sans-serif',
        },
      });
  }
  return graphics;
}
function rectangleMetrics(item: ChartDrawing): string {
  return rectangleMetricsForPoints(item.points);
}
function rectangleMetricsForPoints(points: ChartDrawingPoint[]): string {
  const indexes = points
    .map((point) => nearestCategoryIndex(point.time))
    .filter((index) => index >= 0);
  if (indexes.length < 2) return "";
  const start = Math.min(...indexes);
  const end = Math.max(...indexes);
  const bars = props.bars.slice(start, end + 1);
  const highs = bars
    .map((bar) => asNumber(bar.high))
    .filter((value): value is number => value != null);
  const lows = bars
    .map((bar) => asNumber(bar.low))
    .filter((value): value is number => value != null);
  if (!highs.length || !lows.length) return `K线数量 ${bars.length}`;
  const high = Math.max(...highs);
  const low = Math.min(...lows);
  const upCount = bars.filter(
    (bar) => (asNumber(bar.close) ?? 0) >= (asNumber(bar.open) ?? 0),
  ).length;
  const downCount = bars.length - upCount;
  const firstBar = bars[0];
  const lastBar = bars[bars.length - 1];
  const openPrice = asNumber(firstBar?.open);
  const closePrice = asNumber(lastBar?.close);
  const pctChange =
    openPrice && closePrice
      ? ((closePrice - openPrice) / openPrice) * 100
      : null;
  const amplitude = openPrice
    ? ((high - low) / Math.abs(openPrice)) * 100
    : null;
  const two = (value: number | null): string =>
    value == null ? "—" : value.toFixed(2);
  return [
    `${bars.length}根K线  ${upCount}根上涨  ${downCount}根下跌  涨幅:${pctChange == null ? "—" : `${pctChange.toFixed(2)}%`}  振幅:${amplitude == null ? "—" : `${amplitude.toFixed(2)}%`}`,
    `开盘:${two(openPrice)}  收盘:${two(closePrice)}  最高:${two(high)}  最低:${two(low)}`,
  ].join("\n");
}
function rectangleBounds(
  item: ChartDrawing,
): { high: number; low: number } | null {
  return rectangleBoundsForPoints(item.points);
}
function rectangleBoundsForPoints(
  points: ChartDrawingPoint[],
): { high: number; low: number } | null {
  const indexes = points
    .map((point) => nearestCategoryIndex(point.time))
    .filter((index) => index >= 0);
  if (indexes.length < 2) return null;
  const start = Math.min(...indexes);
  const end = Math.max(...indexes);
  const bars = props.bars.slice(start, end + 1);
  const highs = bars
    .map((bar) => asNumber(bar.high))
    .filter((value): value is number => value != null);
  const lows = bars
    .map((bar) => asNumber(bar.low))
    .filter((value): value is number => value != null);
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
  return Array.from(text).reduce(
    (total, character) => total + (character.charCodeAt(0) > 255 ? 10 : 5.5),
    0,
  );
}
function readableTextColor(color: string): string {
  const rgba = color.match(/^rgba?\(([^)]+)\)$/i);
  const parts =
    rgba?.[1]
      .split(/[,/]/)
      .map((part) => Number.parseFloat(part.trim()))
      .filter(Number.isFinite) ?? [];
  const hex = color.replace("#", "");
  const rgb =
    parts.length >= 3
      ? parts.slice(0, 3)
      : /^[0-9a-f]{6}$/i.test(hex)
        ? [0, 2, 4].map((offset) =>
            Number.parseInt(hex.slice(offset, offset + 2), 16),
          )
        : /^[0-9a-f]{3}$/i.test(hex)
          ? hex
              .split("")
              .map((character) =>
                Number.parseInt(`${character}${character}`, 16),
              )
          : [41, 98, 255];
  const [red, green, blue] = rgb;
  const luminance =
    (Number(red) * 299 + Number(green) * 587 + Number(blue) * 114) / 255000;
  return luminance > 0.68 ? "#17202a" : "#ffffff";
}
function rectangleLayout(points: ChartDrawingPoint[]): {
  first: number[];
  second: number[];
  x: number;
  y: number;
  width: number;
  height: number;
  labelWidth: number;
  labelX: number;
  labelY: number;
} | null {
  const plot = plotRect();
  const first = drawingPoint(points[0]);
  const second = drawingPoint(points[1]);
  if (!plot || !first || !second) return null;
  const x = clamp(Math.min(first[0], second[0]), plot.left, plot.right);
  const endX = clamp(Math.max(first[0], second[0]), plot.left, plot.right);
  const autoBounds = rectangleBoundsForPoints(points);
  let topPix = first[1];
  let bottomPix = second[1];
  if (autoBounds && chart) {
    const highPix = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
      0,
      autoBounds.high,
    ]);
    const lowPix = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
      0,
      autoBounds.low,
    ]);
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
  const measuredWidth = Math.max(
    ...rectangleMetricsForPoints(points).split("\n").map(measureTextWidth),
  );
  const labelWidth = Math.min(plotWidth, Math.max(120, measuredWidth + 14));
  const belowSpace = plot.bottom - boxBottom - RECTANGLE_LABEL_GAP;
  const aboveSpace = y - plot.top - RECTANGLE_LABEL_GAP;
  const labelAbove =
    belowSpace < RECTANGLE_LABEL_HEIGHT && aboveSpace >= belowSpace;
  const requestedLabelY = labelAbove
    ? y - RECTANGLE_LABEL_HEIGHT - RECTANGLE_LABEL_GAP
    : boxBottom + RECTANGLE_LABEL_GAP;
  return {
    first,
    second,
    ...box,
    labelWidth,
    labelX: clamp(
      box.x,
      plot.left,
      Math.max(plot.left, plot.right - labelWidth),
    ),
    labelY: clamp(
      requestedLabelY,
      plot.top,
      Math.max(plot.top, plot.bottom - RECTANGLE_LABEL_HEIGHT),
    ),
  };
}
function drawingGraphics(): object[] {
  if (!chart) return [];
  const palette = theme.palette;
  const plot = plotRect();
  if (!plot) return [];
  const graphics = props.drawings
    .filter((item) => !item.hidden)
    .flatMap<object>((item): object[] => {
      const activeItem =
        rectangleDrag?.drawingId === item.id
          ? { ...item, points: rectangleDrag.points }
          : fibonacciDrag?.drawingId === item.id
            ? { ...item, points: fibonacciDrag.points }
            : item;
      const first = activeItem.points[0] && drawingPoint(activeItem.points[0]);
      if (!first) return [];
      const style = activeItem.style ?? {};
      const color = style.color || palette.accent;
      const width = style.width ?? 1.5;
      const dash = lineDash(style.lineStyle);
      const selected = props.selectedDrawingId === item.id;
      const selectAnchor = (params: unknown) => {
        const raw = params as {
          offsetX?: number;
          offsetY?: number;
          event?: { offsetX?: number; offsetY?: number };
        };
        const left = raw.offsetX ?? raw.event?.offsetX;
        const top = raw.offsetY ?? raw.event?.offsetY;
        emit(
          "selectDrawing",
          item.id,
          typeof left === "number" && typeof top === "number"
            ? { left, top }
            : undefined,
        );
      };
      const common = {
        id: `draw_${item.id}`,
        silent: props.drawingsReadOnly,
        z: selected ? 140 : 120,
        onclick: props.drawingsReadOnly ? undefined : selectAnchor,
        ...dragOptions(item),
        style: {
          stroke: color,
          lineWidth: selected ? width + 1 : width,
          lineDash: dash,
        },
      };
      if (item.type === "horizontal")
        return [
          {
            type: "line",
            ...common,
            shape: {
              x1: plot.left,
              y1: clamp(first[1], plot.top, plot.bottom),
              x2: plot.right,
              y2: clamp(first[1], plot.top, plot.bottom),
            },
          },
          {
            type: "text",
            silent: true,
            z: 141,
            style: {
              x: plot.right - 4,
              y: clamp(first[1] - 8, plot.top, plot.bottom - 16),
              text: format(activeItem.points[0].price),
              textAlign: "right",
              fill: color,
              backgroundColor: palette.background,
              padding: [2, 4],
            },
          },
        ];
      if (item.type === "vertical")
        return [
          {
            type: "line",
            ...common,
            shape: {
              x1: clamp(first[0], plot.left, plot.right),
              y1: plot.top,
              x2: clamp(first[0], plot.left, plot.right),
              y2: plot.bottom,
            },
          },
          {
            type: "text",
            silent: true,
            z: 141,
            style: {
              x: clamp(first[0], plot.left + 36, plot.right - 36),
              y: plot.bottom - 4,
              text: timeLabel(activeItem.points[0].time),
              textAlign: "center",
              textVerticalAlign: "bottom",
              fill: color,
              backgroundColor: palette.background,
              padding: [2, 4],
            },
          },
        ];
      if (item.type === "text") {
        return [
          {
            type: "text",
            ...common,
            style: {
              x: clamp(first[0], plot.left, plot.right - 20),
              y: clamp(first[1], plot.top, plot.bottom - 20),
              text: activeItem.text || "文本框",
              fill: style.color || palette.textPrimary,
              font: `bold ${style.fontSize ?? 14}px SimHei, sans-serif`,
            },
            ondblclick: props.drawingsReadOnly
              ? undefined
              : () => beginTextEdit(item, first),
          },
        ];
      }
      if (item.type === "fibonacci_retracement") {
        const second =
          activeItem.points[1] && drawingPoint(activeItem.points[1]);
        if (!second) return [];
        const left = clamp(
          Math.min(first[0], second[0]),
          plot.left,
          plot.right,
        );
        const right = clamp(
          Math.max(first[0], second[0]),
          plot.left,
          plot.right,
        );
        const levels = fibonacciLevels(activeItem)
          .map((level) => ({
            ...level,
            price: fibonacciRetracementPrice(
              activeItem.points[0].price,
              activeItem.points[1].price,
              level.ratio,
            ),
          }))
          .map((level) => {
            const pixel = chart!.convertToPixel(
              { xAxisIndex: 0, yAxisIndex: 0 },
              [range.value.end, level.price],
            );
            return Array.isArray(pixel)
              ? { ...level, y: clamp(Number(pixel[1]), plot.top, plot.bottom) }
              : null;
          })
          .filter(
            (level): level is FibonacciRetracementLevel & { price: number; y: number } =>
              level !== null,
          );
        if (!levels.length) return [];
        const top = Math.min(...levels.map((level) => level.y), first[1], second[1]);
        const bottom = Math.max(...levels.map((level) => level.y), first[1], second[1]);
        return [
          ...levels.flatMap<object>((level, index) => [
            {
              id: `draw_${item.id}_level_${index}`,
              type: "line",
              silent: true,
              z: selected ? 140 : 120,
              shape: { x1: left, y1: level.y, x2: right, y2: level.y },
              style: {
                stroke: color,
                lineWidth: selected ? width + 1 : width,
                lineDash: dash,
              },
            },
            {
              id: `draw_${item.id}_label_${index}`,
              type: "text",
              silent: true,
              z: selected ? 141 : 121,
              style: {
                x: clamp(right - 4, plot.left + 4, plot.right - 4),
                y: level.y,
                text: `${level.label}  ${format(level.price)}`,
                textAlign: "right",
                textVerticalAlign: "middle",
                fill: color,
                backgroundColor: alpha(palette.background, 0.78),
                padding: [1, 3],
                font: '11px "Microsoft YaHei UI", sans-serif',
              },
            },
          ]),
          {
            type: "rect",
            ...common,
            shape: {
              x: left,
              y: clamp(Math.min(top, bottom), plot.top, plot.bottom),
              width: Math.max(4, right - left),
              height: Math.max(6, Math.abs(bottom - top)),
            },
            style: { fill: "rgba(0,0,0,0)" },
          },
          ...(selected
            ? [
                {
                  id: `draw_${item.id}_start`,
                  type: "circle",
                  silent: false,
                  z: 145,
                  shape: {
                    cx: clamp(first[0], plot.left, plot.right),
                    cy: clamp(first[1], plot.top, plot.bottom),
                    r: 5,
                  },
                  style: { fill: "#ffffff", stroke: color, lineWidth: 2 },
                  onclick: selectAnchor,
                  cursor: "move",
                },
                {
                  id: `draw_${item.id}_end`,
                  type: "circle",
                  silent: false,
                  z: 145,
                  shape: {
                    cx: clamp(second[0], plot.left, plot.right),
                    cy: clamp(second[1], plot.top, plot.bottom),
                    r: 5,
                  },
                  style: { fill: "#ffffff", stroke: color, lineWidth: 2 },
                  onclick: selectAnchor,
                  cursor: "move",
                },
              ]
            : []),
        ];
      }
      if (item.type === "brush" || item.type === "trend") {
        const points = activeItem.points
          .map(drawingPoint)
          .filter((point): point is number[] => point != null);
        return points.length < 2
          ? []
          : [
              {
                type: "polyline",
                ...common,
                shape: { points },
                style: common.style,
              },
              ...(props.drawingsReadOnly
                ? []
                : [
                    {
                      id: `draw_${item.id}_hit`,
                      type: "polyline",
                      z: 119,
                      shape: { points },
                      style: {
                        stroke: "rgba(0,0,0,0)",
                        lineWidth: Math.max(6, width + 4),
                      },
                      onclick: selectAnchor,
                      ...dragOptions(item),
                    },
                  ]),
            ];
      }
      if (item.type === 'long_position' || item.type === 'short_position') return [];
      const layout = rectangleLayout(activeItem.points);
      if (!layout) return [];
      return [
        {
          type: "rect",
          ...common,
          shape: {
            x: layout.x,
            y: layout.y,
            width: layout.width,
            height: layout.height,
          },
          style: {
            ...common.style,
            fill: alpha(style.fillColor || color, style.fillOpacity ?? 0.1),
          },
        },
        {
          type: "text",
          silent: true,
          z: 141,
          style: {
            x: layout.labelX,
            y: layout.labelY,
            text: rectangleMetricsForPoints(activeItem.points),
            fill: readableTextColor(color),
            backgroundColor: color,
            padding: [3, 5],
            lineHeight: 17,
            font: RECTANGLE_LABEL_FONT,
            fontWeight: 400,
          },
        },
        ...(selected
          ? [
              {
                id: `draw_${item.id}_start`,
                type: "circle",
                silent: false,
                z: 145,
                shape: {
                  cx: clamp(layout.first[0], plot.left, plot.right),
                  cy: layout.y + layout.height / 2,
                  r: 5,
                },
                style: { fill: "#ffffff", stroke: color, lineWidth: 2 },
                onclick: selectAnchor,
                cursor: "ew-resize",
              },
              {
                id: `draw_${item.id}_end`,
                type: "circle",
                silent: false,
                z: 145,
                shape: {
                  cx: clamp(layout.second[0], plot.left, plot.right),
                  cy: layout.y + layout.height / 2,
                  r: 5,
                },
                style: { fill: "#ffffff", stroke: color, lineWidth: 2 },
                onclick: selectAnchor,
                cursor: "ew-resize",
              },
            ]
          : []),
      ];
    });
  for (const item of props.drawings.filter((d) => !d.hidden && ['long_position', 'short_position'].includes(d.type))) {
    const pixels = item.points.map(drawingPoint);
    if (pixels.length !== 3 || !pixels.every(Boolean)) continue;
    const [entry, stop, target] = pixels as number[][];
    const x = Math.min(entry[0], target[0]), width = Math.max(35, Math.abs(target[0] - entry[0]));
    const risk = Math.abs(item.points[0].price - item.points[1].price);
    const reward = Math.abs(item.points[2].price - item.points[0].price);
    graphics.push({id: 'draw_' + item.id + '_risk', type: 'group', z: 145, onclick: () => emit('selectDrawing', item.id), ...dragOptions(item), children: [
      {id:'draw_'+item.id+'_stop',type:'rect', shape:{x,y:Math.min(entry[1],stop[1]),width,height:Math.abs(entry[1]-stop[1])},style:{fill:'#ef444433',stroke:'#ef4444'}},
      {id:'draw_'+item.id+'_target',type:'rect', shape:{x,y:Math.min(entry[1],target[1]),width,height:Math.abs(entry[1]-target[1])},style:{fill:'#10b98133',stroke:'#10b981'}},
      {id:'draw_'+item.id+'_label',type:'text',style:{x:x+5,y:entry[1]-15,text:(item.type === 'long_position' ? '多头' : '空头') + ' 盈亏比 ' + (risk ? (reward/risk).toFixed(2) : '—'),fill:'#64748b',fontSize:12}},
    ]});
  }
  if (props.drawingTool === "trend" && rectangleAnchor.value && rectangleCursor.value) {
    const points = [rectangleAnchor.value, rectangleCursor.value].map(drawingPoint);
    if (points.every(Boolean)) graphics.push({ id: "trend_preview", type: "polyline", silent: true, z: 149, shape: { points }, style: { stroke: DEFAULT_DRAWING_COLOR, lineWidth: 1.5, lineDash: [6, 4] } });
  }
  if (props.drawingTool === "rectangle" && rectangleCursor.value) {
    const cursorPoint = drawingPoint(rectangleCursor.value);
    if (cursorPoint) {
      const previewColor = DEFAULT_DRAWING_COLOR;
      const previewDot = (pixel: number[], id: string) => ({
        id,
        type: "circle",
        silent: true,
        z: 150,
        shape: {
          cx: clamp(pixel[0], plot.left, plot.right),
          cy: clamp(pixel[1], plot.top, plot.bottom),
          r: 5,
        },
        style: { fill: "#ffffff", stroke: previewColor, lineWidth: 2 },
      });
      if (!rectangleAnchor.value) {
        graphics.push(previewDot(cursorPoint, "rectangle_preview_cursor"));
      } else {
        const previewLayout = rectangleLayout([
          rectangleAnchor.value,
          rectangleCursor.value,
        ]);
        const anchorPoint = drawingPoint(rectangleAnchor.value);
        if (previewLayout && anchorPoint) {
          graphics.push({
            id: "rectangle_preview_rect",
            type: "rect",
            silent: true,
            z: 149,
            shape: {
              x: previewLayout.x,
              y: previewLayout.y,
              width: previewLayout.width,
              height: previewLayout.height,
            },
            style: {
              fill: alpha(previewColor, 0.08),
              stroke: previewColor,
              lineWidth: 1.5,
              lineDash: [6, 4],
            },
          });
          graphics.push(previewDot(anchorPoint, "rectangle_preview_anchor"));
          graphics.push(previewDot(cursorPoint, "rectangle_preview_cursor"));
        }
      }
    }
  }
  if (props.drawingTool === "fibonacci_retracement" && fibonacciCursor.value) {
    const cursorPoint = drawingPoint(fibonacciCursor.value);
    if (cursorPoint) {
      const previewColor = DEFAULT_DRAWING_COLOR;
      if (!fibonacciAnchor.value) {
        graphics.push({
          id: "fibonacci_preview_cursor",
          type: "circle",
          silent: true,
          z: 150,
          shape: {
            cx: clamp(cursorPoint[0], plot.left, plot.right),
            cy: clamp(cursorPoint[1], plot.top, plot.bottom),
            r: 5,
          },
          style: { fill: "#ffffff", stroke: previewColor, lineWidth: 2 },
        });
      } else {
        const anchorPoint = drawingPoint(fibonacciAnchor.value);
        if (anchorPoint) {
          const left = clamp(
            Math.min(anchorPoint[0], cursorPoint[0]),
            plot.left,
            plot.right,
          );
          const right = clamp(
            Math.max(anchorPoint[0], cursorPoint[0]),
            plot.left,
            plot.right,
          );
          for (const level of DEFAULT_FIBONACCI_RETRACEMENT_LEVELS) {
            const price = fibonacciRetracementPrice(
              fibonacciAnchor.value.price,
              fibonacciCursor.value.price,
              level.ratio,
            );
            const pixel = chart!.convertToPixel(
              { xAxisIndex: 0, yAxisIndex: 0 },
              [range.value.end, price],
            );
            if (!Array.isArray(pixel)) continue;
            const y = clamp(Number(pixel[1]), plot.top, plot.bottom);
            graphics.push({
              id: `fibonacci_preview_${level.ratio}`,
              type: "line",
              silent: true,
              z: 149,
              shape: { x1: left, y1: y, x2: right, y2: y },
              style: { stroke: previewColor, lineWidth: 1.5, lineDash: [6, 4] },
            });
          }
          for (const [id, point] of [
            ["fibonacci_preview_anchor", anchorPoint],
            ["fibonacci_preview_cursor", cursorPoint],
          ] as const)
            graphics.push({
              id,
              type: "circle",
              silent: true,
              z: 150,
              shape: {
                cx: clamp(point[0], plot.left, plot.right),
                cy: clamp(point[1], plot.top, plot.bottom),
                r: 5,
              },
              style: { fill: "#ffffff", stroke: previewColor, lineWidth: 2 },
            });
        }
      }
    }
  }
  if (props.drawingTool === "brush" && brushDraft?.points.length) {
    const points = brushDraft.points
      .map(drawingPoint)
      .filter((point): point is number[] => point != null);
    const style = props.brushStyle ?? {};
    if (points.length > 1)
      graphics.push({
        id: "brush_preview",
        type: "polyline",
        silent: true,
        z: 150,
        shape: { points },
        style: {
          stroke: style.color || DEFAULT_DRAWING_COLOR,
          lineWidth: style.width ?? 1.5,
          lineDash: lineDash(style.lineStyle),
          fill: "none",
        },
      });
  }
  return graphics;
}

function beginTextEdit(item: ChartDrawing, pixel?: number[]): void {
  const point = item.points[0];
  const anchor = pixel ?? drawingPoint(point);
  if (!point || !anchor) return;
  textDraft.value = {
    point,
    left: Number(anchor[0]),
    top: Number(anchor[1]),
    value: item.text || "",
    editingId: item.id,
  };
  void nextTick(() => {
    textInput.value?.focus();
    textInput.value?.select();
  });
}

function indicatorSeries(): object[] {
  const palette = theme.palette;
  const line = (
    name: string,
    key: string,
    color: string,
    yAxisIndex = 0,
  ): object | null => {
    const data = props.indicators[key];
    return data?.length
      ? {
          name,
          type: "line",
          data,
          yAxisIndex,
          showSymbol: false,
          connectNulls: false,
          lineStyle: { width: 1.4, color },
          emphasis: { disabled: true },
        }
      : null;
  };
  const legacy = [
    line("MA", "ma", "#f59e0b"),
    line("HSAR 阻力", "hsarResistance", "#ef4444"),
    line("HSAR 支撑", "hsarSupport", "#22c55e"),
    line("布林上轨", "bollingerUpper", "#a855f7"),
    line("布林中轨", "bollingerMiddle", "#a855f7"),
    line("布林下轨", "bollingerLower", "#a855f7"),
    line("ATR 上轨", "atrUpper", "#0ea5e9"),
    line("ATR 中线", "atrMiddle", "#0ea5e9"),
    line("ATR 下轨", "atrLower", "#0ea5e9"),
    line("SD", "sd", palette.chartAxis, 0),
  ].filter((item): item is object => item !== null);
  const colors = [
    "#f59e0b",
    "#3b82f6",
    "#a855f7",
    "#22c55e",
    "#ef4444",
    "#06b6d4",
    "#ec4899",
  ];
  const paneIndexes = new Map(
    paneIndicatorInstances.value.map((item, index) => [
      item.instanceId,
      index + 2,
    ]),
  );
  const dynamic: object[] = props.indicatorInstances
    .filter((item) => item.visible && item.status === "ready")
    .flatMap<object>((instance, instanceIndex) => {
      const gridIndex =
        instance.placement === "pane"
          ? paneIndexes.get(instance.instanceId)
          : 0;
      if (gridIndex == null) return [];
      const yAxisIndex = instance.placement === "pane" ? gridIndex + 1 : 0;
      return (
        instance.plots ??
        Object.keys(instance.series ?? {}).map((id) => ({
          id,
          type: "line" as const,
        }))
      ).flatMap<object>((plot, plotIndex) => {
        const data = instance.series?.[plot.id];
        if (!data?.length) return [];
        const plotStyle = instance.style.plotStyles?.[plot.id] ?? {};
        const color =
          plotStyle.color ??
          instance.style.color ??
          colors[(instanceIndex + plotIndex) % colors.length];
        const common = {
          name: `${instance.displayName || instance.definitionId} · ${plot.id}`,
          xAxisIndex: gridIndex,
          yAxisIndex,
          data,
          emphasis: { disabled: true },
          itemStyle: { color },
        };
        if (plot.type === "histogram")
          return [{ ...common, type: "bar", barMaxWidth: 8 }];
        return [
          {
            ...common,
            type: "line",
            showSymbol: false,
            connectNulls: false,
            lineStyle: {
              width: plotStyle.lineWidth ?? instance.style.lineWidth ?? 1.5,
              type: plotStyle.lineType ?? instance.style.lineType ?? "solid",
              color,
            },
          },
        ];
      });
    });
  return [...legacy, ...dynamic];
}

function chartLayout(): {
  grids: object[];
  xAxes: object[];
  yAxes: object[];
  titles: object[];
} {
  const palette = theme.palette;
  const compact = props.compact;
  const left = compact ? 48 : 66;
  const right = compact ? 42 : 48;
  const top = props.hideQuote ? 16 : displayQuotePanel.value ? (compact ? 54 : 58) : compact ? 26 : 42;
  const bottom = compact ? 20 : 28;
  const paneCount = paneIndicatorInstances.value.length;
  const secondaryCount = paneCount + 1;
  const gap = compact ? CHART_LAYOUT.paneGap - 6 : CHART_LAYOUT.paneGap;
  const available = Math.max(
    120,
    layoutHeight.value - top - bottom - gap * secondaryCount,
  );
  const priceHeight = paneCount
    ? Math.max(130, Math.floor(available * 0.5))
    : Math.floor(available * (compact ? 0.62 : 0.64));
  const secondaryHeight = Math.max(
    42,
    Math.floor((available - priceHeight) / secondaryCount),
  );
  const grids: object[] = [{ left, right, top, height: priceHeight }];
  let cursor = top + priceHeight + gap;
  for (let index = 0; index < secondaryCount; index += 1) {
    grids.push({ left, right, top: cursor, height: secondaryHeight });
    cursor += secondaryHeight + gap;
  }
  const axisBase = {
    type: "category",
    data: categories.value,
    boundaryGap: true,
    axisLine: { lineStyle: { color: palette.chartGrid } },
    axisTick: { show: false },
    splitLine: {
      show: true,
      interval: (index: number) => isMajorTick(index),
      lineStyle: { color: palette.chartGrid, opacity: 0.8 },
    },
    axisPointer: {
      show: true,
      label: {
        formatter: (params: { value: unknown }) =>
          timeLabel(String(params.value ?? "")),
      },
    },
  };
  const xAxes = grids.map((_grid, index) => ({
    ...axisBase,
    gridIndex: index,
    axisLabel: {
      show: index === grids.length - 1,
      interval: (tick: number) => isMajorTick(tick),
      hideOverlap: true,
      color: palette.chartAxis,
      fontSize: compact ? 9 : 11,
      formatter: (value: string) => axisTimeLabel(value),
    },
  }));
  const indicatorAxis = (gridIndex: number, color = palette.chartAxis) => ({
    gridIndex,
    scale: true,
    splitNumber: 3,
    axisLabel: {
      show: true,
      color,
      fontSize: compact ? 8 : 10,
      showMinLabel: false,
      showMaxLabel: false,
    },
    axisTick: { show: false },
    splitLine: { show: true, lineStyle: { color: palette.chartGrid } },
  });
  const priceAxis = {
      scale: true,
      min: priceBounds().min,
      max: priceBounds().max,
      inverse: props.inverse,
      axisLabel: {
        show: true,
        color: palette.chartAxis,
        fontSize: compact ? 8 : 10,
        showMinLabel: false,
        showMaxLabel: false,
      },
      splitLine: { lineStyle: { color: palette.chartGrid } },
      axisPointer: { show: true },
  };
  const yAxes = [
    priceAxis,
    {
      ...indicatorAxis(1, palette.chartVolume),
      min: 0,
      axisLabel: {
        show: true,
        color: palette.chartVolume,
        fontSize: compact ? 8 : 10,
        showMinLabel: false,
        showMaxLabel: false,
        formatter: (value: number) => compactAxis(value),
      },
    },
    {
      ...indicatorAxis(1, palette.chartSecondary),
      min: 0,
      position: "right",
      axisLabel: {
        show: true,
        color: palette.chartSecondary,
        fontSize: compact ? 8 : 10,
        showMinLabel: false,
        showMaxLabel: false,
        formatter: (value: number) => secondaryAxis(value),
      },
      splitLine: { show: false },
    },
    ...paneIndicatorInstances.value.map((_item, index) =>
      indicatorAxis(index + 2),
    ),
    {
      ...priceAxis,
      position: "right",
      splitLine: { show: false },
      axisPointer: { show: false },
    },
  ];
  const titles = [
    {
      text: "成交量",
      left: left + 4,
      top: Number((grids[1] as { top: number }).top) - 12,
      textStyle: { color: palette.chartVolume, fontSize: compact ? 8 : 10, fontWeight: 600 },
    },
    {
      text: secondaryMetricLabel.value,
      left: left + 48,
      top: Number((grids[1] as { top: number }).top) - 12,
      textStyle: { color: palette.chartSecondary, fontSize: compact ? 8 : 10, fontWeight: 600 },
    },
    ...paneIndicatorInstances.value.map((item, index) => ({
    text: item.displayName || item.definitionId,
    left: left + 4,
    top: Number((grids[index + 2] as { top: number }).top) + 2,
    textStyle: {
      color: palette.textSecondary,
      fontSize: compact ? 8 : 10,
      fontWeight: 500,
    },
    })),
  ];
  return { grids, xAxes, yAxes, titles };
}

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  if (!props.bars.length) {
    chart.clear();
    return;
  }
  const palette = theme.palette;
  const defaultRange = initialRange();
  if (range.value.end >= props.bars.length || range.value.end === 0)
    range.value = defaultRange;
  const up = props.swapColors ? palette.priceDown : palette.priceUp;
  const down = props.swapColors ? palette.priceUp : palette.priceDown;
  const compact = props.compact;
  const layout = chartLayout();
  chart.setOption(
    {
      backgroundColor: "transparent",
      animation: false,
      axisPointer: {
        show: true,
        link: [{ xAxisIndex: "all" }],
        label: {
          show: !compact,
          backgroundColor: palette.surfaceSelected,
          color: palette.textPrimary,
        },
      },
      tooltip: {
        trigger: "axis",
        showContent: false,
        axisPointer: { type: "cross", snap: true },
      },
      title: layout.titles,
      grid: layout.grids,
      xAxis: layout.xAxes,
      yAxis: layout.yAxes,
      dataZoom: [
        {
          type: "inside",
          xAxisIndex: layout.xAxes.map((_item, index) => index),
          startValue: range.value.start,
          endValue: range.value.end,
          zoomOnMouseWheel: false,
          moveOnMouseWheel: false,
          moveOnMouseMove: false,
          preventDefaultMouseMove: true,
        },
      ],
      series: [
        {
          name: "K线",
          type: props.chartType === "line" || props.chartType === "area" ? "line" : "candlestick",
          data: props.chartType === "line" || props.chartType === "area"
            ? props.bars.map((bar) => asNumber(bar.close))
            : props.chartType === "heikin"
              ? heikinAshi(props.bars).map((bar) => [asNumber(bar.open), asNumber(bar.close), asNumber(bar.low), asNumber(bar.high)])
              : candleData.value,
          showSymbol: false,
          connectNulls: false,
          lineStyle: { color: palette.priceUp, width: 1.5 },
          areaStyle: props.chartType === "area" ? { color: palette.priceUp, opacity: 0.18 } : undefined,
          markPoint: {
            silent: true,
            symbolSize: compact ? 18 : 28,
            label: { show: !compact, fontSize: 9 },
            data: props.strategyMarkers
              .filter(
                (item) =>
                  Number.isInteger(item.barIndex) &&
                  item.barIndex >= 0 &&
                  item.barIndex < props.bars.length &&
                  Number.isFinite(item.price),
              )
              .map((item) => ({
                name: item.label || (item.kind === "entry" ? "开仓" : "平仓"),
                value: item.label || (item.kind === "entry" ? "开" : "平"),
                coord: [item.barIndex, item.price],
                symbol: item.kind === "entry" ? "arrow" : "pin",
                symbolRotate: item.kind === "entry" ? 0 : 180,
                itemStyle: {
                  color:
                    item.kind === "entry" ? palette.priceUp : palette.priceDown,
                },
              })),
          },
          itemStyle: {
            color: props.chartType === "hollow" ? "transparent" : up,
            color0: down,
            borderColor: up,
            borderColor0: down,
          },
          emphasis: { disabled: true },
        },
        nestedBarSeries(secondaryMetricLabel.value, secondaryData.value, 2, CHART_LAYOUT.secondaryBackRatio, 1),
        nestedBarSeries("成交量", volumeData.value, 1, CHART_LAYOUT.secondaryFrontRatio, 2),
        ...indicatorSeries(),
      ],
    },
    true,
  );
  renderGraphics();
}

function updateRange(event: unknown): void {
  const batch =
    (
      event as {
        batch?: Array<{
          startValue?: number;
          endValue?: number;
          start?: number;
          end?: number;
        }>;
      }
    ).batch?.[0] ??
    (event as {
      startValue?: number;
      endValue?: number;
      start?: number;
      end?: number;
    });
  const last = Math.max(0, props.bars.length - 1);
  const start =
    typeof batch.startValue === "number"
      ? batch.startValue
      : typeof batch.start === "number"
        ? Math.round((batch.start / 100) * last)
        : range.value.start;
  const end =
    typeof batch.endValue === "number"
      ? batch.endValue
      : typeof batch.end === "number"
        ? Math.round((batch.end / 100) * last)
        : range.value.end;
  range.value = { start, end };
  emit("visibleRange", start, end);
  if (
    start <= 15 &&
    !props.loadingEarlier &&
    requestedEarlierForLength !== props.bars.length
  ) {
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
function coordinateFromEvent(params: {
  event?: { offsetX?: number; offsetY?: number };
}, freehand = false): { time: string; price: number } | null {
  if (!chart || params.event?.offsetX == null || params.event.offsetY == null)
    return null;
  const cursor = [params.event.offsetX, params.event.offsetY];
  if (!chart.containPixel({ gridIndex: 0 }, cursor)) return null;
  const values = chart.convertFromPixel(
    { xAxisIndex: 0, yAxisIndex: 0 },
    cursor,
  );
  if (!Array.isArray(values)) return null;
  const index = Math.max(
    0,
    Math.min(props.bars.length - 1, Math.round(Number(values[0]))),
  );
  let price = Number(values[1]);
  if (freehand && categories.value.length > 1) {
    const center = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [index, price]) as number[];
    const neighbor = cursor[0] < center[0] ? Math.max(0, index - 1) : Math.min(categories.value.length - 1, index + 1);
    const other = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [neighbor, price]) as number[];
    const t0 = Date.parse(categories.value[index]), t1 = Date.parse(categories.value[neighbor]);
    if (neighbor !== index && other[0] !== center[0] && Number.isFinite(t0) && Number.isFinite(t1)) {
      const ratio = clamp((cursor[0] - center[0]) / (other[0] - center[0]), 0, 1);
      return { time: new Date(t0 + (t1 - t0) * ratio).toISOString(), price };
    }
  }
  if (props.magnet && !freehand) {
    const candleCenter = chart.convertToPixel(
      { xAxisIndex: 0, yAxisIndex: 0 },
      [index, price],
    );
    const bar = props.bars[index];
    const candidates = [bar.high, bar.low].filter(
      (value): value is number => asNumber(value) != null,
    );
    if (
      Array.isArray(candleCenter) &&
      Math.abs(Number(candleCenter[0]) - cursor[0]) <= 20 &&
      candidates.length
    ) {
      const nearest = candidates
        .map((candidate) => ({
          candidate,
          pixel: chart!.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [
            index,
            candidate,
          ]),
        }))
        .filter((item) => Array.isArray(item.pixel))
        .sort(
          (left, right) =>
            Math.abs(Number(left.pixel[1]) - cursor[1]) -
            Math.abs(Number(right.pixel[1]) - cursor[1]),
        )[0];
      if (nearest && Math.abs(Number(nearest.pixel[1]) - cursor[1]) <= 20)
        price = nearest.candidate;
    }
  }
  return { time: categories.value[index], price };
}
function chartClick(params: {
  event?: { offsetX?: number; offsetY?: number };
}): void {
  if (props.replayPick) {
    const point = coordinateFromEvent(params);
    if (point) emit("pickBar", nearestCategoryIndex(point.time));
    return;
  }
  if (["cursor", "brush", "laser"].includes(props.drawingTool)) return;
  const point = coordinateFromEvent(params);
  if (!point) return;
  const anchor = {
    left: Number(params.event?.offsetX ?? 0),
    top: Number(params.event?.offsetY ?? 0),
  };
  if (props.drawingTool === "long_position" || props.drawingTool === "short_position") {
    const sign = props.drawingTool === "long_position" ? 1 : -1;
    const entry = riskPoints.value[0];
    if (entry && ((riskPoints.value.length === 1 && sign * (point.price - entry.price) >= 0) || (riskPoints.value.length === 2 && sign * (point.price - entry.price) <= 0))) {
      riskHint.value = riskPoints.value.length === 1 ? "止损参考价方向不正确，请重新选择" : "目标参考价方向不正确，请重新选择"; return;
    }
    riskPoints.value.push(point);
    riskHint.value = riskPoints.value.length === 1 ? "请选择止损参考价" : "请选择目标参考价及图形右边界";
    if (riskPoints.value.length === 3) {
      emit("draw", {type: props.drawingTool, points: [...riskPoints.value]}, anchor);
      riskPoints.value = []; riskHint.value = "";
    }
    return;
  }
  if (props.drawingTool === "rectangle" || props.drawingTool === "trend") {
    if (!rectangleAnchor.value) {
      rectangleAnchor.value = point;
      rectangleCursor.value = point;
      renderGraphics();
      return;
    }
    emit(
      "draw",
      { type: props.drawingTool, points: [rectangleAnchor.value, point] },
      anchor,
    );
    rectangleAnchor.value = undefined;
    rectangleCursor.value = undefined;
    renderGraphics();
    return;
  }
  if (props.drawingTool === "fibonacci_retracement") {
    if (!fibonacciAnchor.value) {
      fibonacciAnchor.value = point;
      fibonacciCursor.value = point;
      renderGraphics();
      return;
    }
    emit(
      "draw",
      {
        type: "fibonacci_retracement",
        version: 1,
        points: [fibonacciAnchor.value, point],
        levels: DEFAULT_FIBONACCI_RETRACEMENT_LEVELS.map((level) => ({
          ...level,
        })),
      },
      anchor,
    );
    fibonacciAnchor.value = undefined;
    fibonacciCursor.value = undefined;
    renderGraphics();
    return;
  }
  if (props.drawingTool === "text") {
    textDraft.value = {
      point,
      left: Number(params.event?.offsetX ?? 0),
      top: Number(params.event?.offsetY ?? 0),
      value: "",
    };
    void nextTick(() => textInput.value?.focus());
    return;
  }
  if (props.drawingTool !== "cursor" && props.drawingTool !== "laser")
    emit("draw", { type: props.drawingTool, points: [point] }, anchor);
}
function commitText(): void {
  const draft = textDraft.value;
  if (!draft) return;
  const value = draft.value.trim();
  textDraft.value = undefined;
  if (!value) return;
  if (draft.editingId) {
    const item = props.drawings.find(
      (drawing) => drawing.id === draft.editingId,
    );
    if (item) emit("updateDrawing", { ...item, text: value });
  } else
    emit(
      "draw",
      { type: "text", points: [draft.point], text: value },
      { left: draft.left, top: draft.top },
    );
}
function cancelText(): void {
  textDraft.value = undefined;
}
function captureBrushPointer(payload: unknown): void {
  const raw = payload as { event?: unknown };
  const nested = raw.event as
    | {
        event?: unknown;
        pointerId?: unknown;
        currentTarget?: unknown;
        target?: unknown;
      }
    | undefined;
  const native = (nested?.event ?? nested) as
    | { pointerId?: unknown; currentTarget?: unknown; target?: unknown }
    | undefined;
  const pointerId = Number(native?.pointerId);
  const target = (native?.currentTarget ?? native?.target) as
    PointerCaptureTarget | undefined;
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
    try {
      brushPointerTarget.releasePointerCapture(brushPointerId);
    } catch {
      /* capture may already be lost */
    }
  }
  brushPointerId = undefined;
  brushPointerTarget = undefined;
}
function pointerDown(event: unknown): void {
  if (props.replayPick) return;
  const payload = event as {
    offsetX?: number;
    offsetY?: number;
    target?: { id?: string };
    event?: { button?: number; preventDefault?: () => void };
  };
  if ((payload.event?.button ?? 0) !== 0 || typeof payload.offsetX !== "number")
    return;
  const item = drawingFromGraphicId(payload.target?.id);
  if (props.drawingTool === "brush") {
    const point = coordinateFromEvent({
      event: { offsetX: payload.offsetX, offsetY: payload.offsetY },
    }, true);
    if (point && typeof payload.offsetY === "number") {
      brushDraft = {
        points: [point],
        lastX: payload.offsetX,
        lastY: payload.offsetY,
        anchor: { left: payload.offsetX, top: payload.offsetY },
      };
      captureBrushPointer(payload);
      payload.event?.preventDefault?.();
    }
    return;
  }
  if (
    props.drawingTool === "cursor" &&
    item?.type === "fibonacci_retracement" &&
    (payload.target?.id === `draw_${item.id}_start` ||
      payload.target?.id === `draw_${item.id}_end`) &&
    !props.drawingsReadOnly &&
    !item.style?.locked
  ) {
    fibonacciDrag = {
      drawingId: item.id,
      pointIndex: payload.target?.id === `draw_${item.id}_start` ? 0 : 1,
      points: item.points.map((point) => ({ ...point })),
    };
    payload.event?.preventDefault?.();
    return;
  }
  if (
    props.drawingTool === "cursor" &&
    item?.type === "rectangle" &&
    !props.drawingsReadOnly &&
    !item.style?.locked
  ) {
    const graphicId = payload.target?.id;
    const mode: RectangleDrag["mode"] =
      graphicId === `draw_${item.id}_start`
        ? "resize-start"
        : graphicId === `draw_${item.id}_end`
          ? "resize-end"
          : "move";
    const originPoints = item.points.map((point) => ({ ...point }));
    rectangleDrag = {
      drawingId: item.id,
      mode,
      startX: payload.offsetX,
      originPoints,
      points: originPoints,
    };
    payload.event?.preventDefault?.();
    return;
  }
  if (props.drawingTool !== "cursor") return;
  if (typeof payload.target?.id === "string" && payload.target.id.startsWith("draw_")) return;
  panStartX = payload.offsetX;
  panOrigin = { ...range.value };
  requestedEarlierInGesture = false;
  payload.event?.preventDefault?.();
}
function pointerMove(event: unknown): void {
  const payload = event as {
    offsetX?: number;
    offsetY?: number;
    event?: { preventDefault?: () => void };
  };
  const x = payload.offsetX;
  if (
    brushDraft &&
    props.drawingTool === "brush" &&
    typeof x === "number" &&
    typeof payload.offsetY === "number"
  ) {
    const distance = Math.hypot(
      x - brushDraft.lastX,
      payload.offsetY - brushDraft.lastY,
    );
    const point =
      distance >= 2
        ? coordinateFromEvent({
            event: { offsetX: x, offsetY: payload.offsetY },
          }, true)
        : null;
    if (point) {
      brushDraft.points.push(point);
      brushDraft.lastX = x;
      brushDraft.lastY = payload.offsetY;
      if (brushDraft.points.length > 2048)
        brushDraft.points = brushDraft.points.filter(
          (_item, index) =>
            index % 2 === 0 || index === brushDraft!.points.length - 1,
        );
      scheduleGraphicsRender();
    }
    payload.event?.preventDefault?.();
    return;
  }
  if (
    fibonacciDrag &&
    props.drawingTool === "cursor" &&
    typeof x === "number" &&
    typeof payload.offsetY === "number"
  ) {
    fibonacciDrag.points = resizedFibonacciPoints(
      fibonacciDrag.points,
      fibonacciDrag.pointIndex,
      x,
      payload.offsetY,
    );
    scheduleGraphicsRender();
    payload.event?.preventDefault?.();
    return;
  }
  if (
    rectangleDrag &&
    props.drawingTool === "cursor" &&
    typeof x === "number" &&
    chart
  ) {
    rectangleDrag.points =
      rectangleDrag.mode === "move"
        ? shiftedRectanglePoints(
            rectangleDrag.originPoints,
            x - rectangleDrag.startX,
          )
        : resizedRectanglePoints(
            rectangleDrag.originPoints,
            rectangleDrag.mode === "resize-start" ? 0 : 1,
            x,
          );
    scheduleGraphicsRender();
    payload.event?.preventDefault?.();
    return;
  }
  if (props.drawingTool === "rectangle" || props.drawingTool === "trend") {
    if (typeof x !== "number" || typeof payload.offsetY !== "number") {
      if (rectangleCursor.value) {
        rectangleCursor.value = undefined;
        scheduleGraphicsRender();
      }
      return;
    }
    const point = coordinateFromEvent({
      event: { offsetX: x, offsetY: payload.offsetY },
    });
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
  if (props.drawingTool === "fibonacci_retracement") {
    if (typeof x !== "number" || typeof payload.offsetY !== "number") {
      if (fibonacciCursor.value) {
        fibonacciCursor.value = undefined;
        scheduleGraphicsRender();
      }
      return;
    }
    const point = coordinateFromEvent({
      event: { offsetX: x, offsetY: payload.offsetY },
    });
    if (point) {
      fibonacciCursor.value = point;
      scheduleGraphicsRender();
    } else if (fibonacciCursor.value) {
      fibonacciCursor.value = undefined;
      scheduleGraphicsRender();
    }
    payload.event?.preventDefault?.();
    return;
  }
  if (panStartX == null || !panOrigin || typeof x !== "number" || !chart)
    return;
  const delta = x - panStartX;
  const visible = Math.max(1, panOrigin.end - panOrigin.start + 1);
  const pixelsPerBar = Math.max(2, chart.getWidth() / visible);
  const shift = Math.round(delta / pixelsPerBar);
  if (
    !requestedEarlierInGesture &&
    panOrigin.start <= 15 &&
    delta >= 24 &&
    !props.loadingEarlier
  ) {
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
    chart.dispatchAction({
      type: "dataZoom",
      startValue: start,
      endValue: end,
    });
  }
  payload.event?.preventDefault?.();
}
function pointerUp(): void {
  if (brushDraft) {
    const draft = brushDraft;
    brushDraft = undefined;
    releaseBrushPointer();
    const points = simplifyBrush(draft.points);
    if (points.length >= 2)
      emit("draw", { type: "brush", points }, draft.anchor);
    scheduleGraphicsRender();
  }
  if (rectangleDrag) {
    const item = props.drawings.find(
      (drawing) => drawing.id === rectangleDrag?.drawingId,
    );
    const points = rectangleDrag.points;
    rectangleDrag = undefined;
    if (item) emit("updateDrawing", { ...item, points });
    scheduleGraphicsRender();
  }
  if (fibonacciDrag) {
    const item = props.drawings.find(
      (drawing) => drawing.id === fibonacciDrag?.drawingId,
    );
    const points = fibonacciDrag.points;
    fibonacciDrag = undefined;
    if (item) emit("updateDrawing", { ...item, points });
    scheduleGraphicsRender();
  }
  panStartX = undefined;
  panOrigin = undefined;
  requestedEarlierInGesture = false;
}
function pointerOut(): void {
  if (rectangleCursor.value) {
    rectangleCursor.value = undefined;
    scheduleGraphicsRender();
  }
  if (fibonacciCursor.value) {
    fibonacciCursor.value = undefined;
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
  if (
    !brushDraft ||
    (brushPointerId != null && event.pointerId !== brushPointerId)
  )
    return;
  pointerUp();
}
let lastDrawingClickSource: unknown;
function drawingCanvasClick(event: unknown): void {
  const payload = event as {
    offsetX?: number;
    offsetY?: number;
    target?: { id?: string };
    source?: unknown;
  };
  if (typeof payload.target?.id === "string" && payload.target.id.startsWith("draw_")) return;
  if (props.drawingTool === "cursor" && !props.replayPick) {
    emit("selectDrawing", "");
    return;
  }
  const source = payload.source ?? payload;
  if (lastDrawingClickSource === source) return;
  lastDrawingClickSource = source;
  chartClick({ event: payload });
}
function seriesCanvasClick(params: unknown): void {
  const raw = params as {
    offsetX?: number;
    offsetY?: number;
    event?: { offsetX?: number; offsetY?: number; target?: { id?: string } };
  };
  drawingCanvasClick({
    offsetX: raw.offsetX ?? raw.event?.offsetX,
    offsetY: raw.offsetY ?? raw.event?.offsetY,
    target: raw.event?.target,
    source: raw.event ?? params,
  });
}
function wheelZoom(event: unknown): void {
  if (!chart || props.bars.length < 2 || brushDraft) return;
  const payload = event as {
    offsetX?: number;
    offsetY?: number;
    wheelDelta?: number;
    event?: {
      wheelDelta?: number;
      deltaY?: number;
      preventDefault?: () => void;
      stopPropagation?: () => void;
    };
  };
  const cursor = [Number(payload.offsetX ?? 0), Number(payload.offsetY ?? 0)];
  if (
    !Array.from(
      { length: 2 + paneIndicatorInstances.value.length },
      (_item, index) => index,
    ).some((gridIndex) => chart!.containPixel({ gridIndex }, cursor))
  )
    return;
  const wheelDelta = Number(
    payload.wheelDelta ??
      payload.event?.wheelDelta ??
      -(payload.event?.deltaY ?? 0),
  );
  if (!wheelDelta) return;
  const currentVisible = Math.max(2, range.value.end - range.value.start + 1);
  const targetVisible = clamp(
    Math.round(currentVisible * (wheelDelta > 0 ? 0.82 : 1.22)),
    8,
    props.bars.length,
  );
  const converted = chart.convertFromPixel({ xAxisIndex: 0 }, cursor);
  const anchor = Array.isArray(converted)
    ? clamp(Math.round(Number(converted[0])), 0, props.bars.length - 1)
    : Math.round((range.value.start + range.value.end) / 2);
  const ratio =
    currentVisible <= 1
      ? 0.5
      : clamp((anchor - range.value.start) / (currentVisible - 1), 0, 1);
  let start = Math.round(anchor - ratio * (targetVisible - 1));
  start = clamp(start, 0, Math.max(0, props.bars.length - targetVisible));
  const end = Math.min(props.bars.length - 1, start + targetVisible - 1);
  chart.dispatchAction({ type: "dataZoom", startValue: start, endValue: end });
  payload.event?.preventDefault?.();
  payload.event?.stopPropagation?.();
}
function installHandlers(): void {
  if (!chart) return;
  chart.off("updateAxisPointer");
  chart.off("datazoom");
  chart.off("click");
  chart.on("updateAxisPointer", (event: unknown) => {
    const value = (event as { axesInfo?: Array<{ value?: number }> })
      .axesInfo?.[0]?.value;
    pendingHoverIndex = typeof value === "number" ? value : -1;
    if (hoverFrame != null) return;
    hoverFrame = requestAnimationFrame(() => {
      hoverFrame = undefined;
      if (hoverIndex.value === pendingHoverIndex) return;
      hoverIndex.value = pendingHoverIndex;
      emit("hover", hoverBar.value);
    });
  });
  chart.on("datazoom", updateRange);
  chart.on("click", seriesCanvasClick);
  const renderer = chart.getZr();
  renderer.off("mousedown", pointerDown);
  renderer.off("mousemove", pointerMove);
  renderer.off("mouseup", pointerUp);
  renderer.off("globalout", pointerOut);
  renderer.off("click", drawingCanvasClick);
  renderer.off("mousewheel", wheelZoom);
  renderer.on("mousedown", pointerDown);
  renderer.on("mousemove", pointerMove);
  renderer.on("mouseup", pointerUp);
  renderer.on("globalout", pointerOut);
  renderer.on("click", drawingCanvasClick);
  renderer.on("mousewheel", wheelZoom);
}
function resize(): void {
  const measured = Math.max(1, Math.round(element.value?.clientHeight || props.height));
  const changed = layoutHeight.value !== measured;
  layoutHeight.value = measured;
  chart?.resize();
  if (changed) void nextTick(render);
}
watch(
  () => props.drawingTool,
  () => {
    riskPoints.value = []; riskHint.value = "";
    rectangleAnchor.value = undefined;
    rectangleCursor.value = undefined;
    fibonacciAnchor.value = undefined;
    fibonacciCursor.value = undefined;
    rectangleDrag = undefined;
    fibonacciDrag = undefined;
    brushDraft = undefined;
    releaseBrushPointer();
    textDraft.value = undefined;
  },
);
watch(
  () => props.inverse,
  () =>
    void nextTick(() => {
      render();
      installHandlers();
    }),
);
watch(
  () => props.swapColors,
  () =>
    void nextTick(() => {
      render();
      installHandlers();
    }),
);
watch(
  () => props.bars,
  (next, previous) => {
    const previousFirst =
      previous?.[0] &&
      String(previous[0].barOpenTime || previous[0].tradingDate || "");
    const prepended =
      previousFirst && next.length > previous.length
        ? next.findIndex(
            (bar) =>
              String(bar.barOpenTime || bar.tradingDate || "") ===
              previousFirst,
          )
        : -1;
    if (prepended > 0) {
      const reveal = Math.min(prepended, pendingEarlierShift);
      range.value = {
        start: Math.max(0, range.value.start + prepended - reveal),
        end: Math.max(0, range.value.end + prepended - reveal),
      };
      pendingEarlierShift = 0;
    } else range.value = initialRange();
    void nextTick(() =>
      emit("visibleRange", range.value.start, range.value.end),
    );
  },
  { deep: false },
);
watch(
  () => props.height,
  (height) => {
    if (!props.fill) layoutHeight.value = height;
    void nextTick(render);
  },
);
watch(
  () => [
    props.bars,
    props.indicators,
    props.indicatorInstances,
    props.volumeProfile,
    props.strategyMarkers,
    props.inverse,
    props.swapColors,
    props.chartType,
    props.drawings,
    props.selectedDrawingId,
    theme.palette,
  ],
  () =>
    void nextTick(() => {
      render();
      installHandlers();
    }),
  { deep: true },
);
onMounted(() => {
  void nextTick(() => {
    layoutHeight.value = Math.max(1, Math.round(element.value?.clientHeight || props.height));
    render();
    installHandlers();
    emit("visibleRange", range.value.start, range.value.end);
  });
  window.addEventListener("resize", resize);
  window.addEventListener("keydown", cancelBrushOnEscape);
  element.value?.addEventListener("pointercancel", pointerCancel);
  if (element.value && typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(element.value);
  }
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize);
  window.removeEventListener("keydown", cancelBrushOnEscape);
  element.value?.removeEventListener("pointercancel", pointerCancel);
  releaseBrushPointer();
  resizeObserver?.disconnect();
  if (rangeRenderTimer) clearTimeout(rangeRenderTimer);
  if (rectangleRenderFrame != null) cancelAnimationFrame(rectangleRenderFrame);
  if (hoverFrame != null) cancelAnimationFrame(hoverFrame);
  chart?.dispose();
  chart = undefined;
});
</script>

<template>
  <div
    class="kline-chart chart-box"
    :class="{ compact, fill, 'drawing-active': drawingTool !== 'cursor', 'cursor-tool': drawingTool === 'cursor' }"
    :style="fill ? undefined : { height: `${height}px` }"
  >
    <div v-if="!hideQuote && hoverBar" class="quote-panel">
      <QuoteValues :bar="hoverBar" :latest-bar="bars.at(-1)" :total-market-cap="totalMarketCap" :float-market-cap="floatMarketCap" :swap-colors="swapColors" />
    </div>
    <div class="chart-overlay-controls"><slot name="overlay" /></div>
    <span v-if="drawingTool === 'long_position' || drawingTool === 'short_position'" class="risk-drawing-hint">{{ riskHint || '请选择开仓参考价，然后选择止损、目标参考价' }}</span>
    <div
      ref="element"
      class="chart-root"
      @dblclick="requestDetail"
      :data-chart-type="chartType"
      :data-layout-height="layoutHeight"
      data-price-axis-count="2"
      :data-rectangle-preview="Boolean(rectangleCursor)"
      :data-rectangle-anchor="Boolean(rectangleAnchor)"
      :data-fibonacci-preview="Boolean(fibonacciCursor)"
      :data-fibonacci-anchor="Boolean(fibonacciAnchor)"
      :data-drawing-count="drawings.filter((item) => !item.hidden).length"
      :data-fibonacci-count="drawings.filter((item) => item.type === 'fibonacci_retracement' && !item.hidden).length"
      :data-indicator-count="
        indicatorInstances.filter(
          (item) => item.visible && item.status === 'ready',
        ).length
      "
      :data-indicator-pane-count="paneIndicatorInstances.length"
      :data-strategy-marker-count="strategyMarkers.length"
      :data-volume-profile-bucket-count="volumeProfile?.buckets.length || 0"
    />
    <LaserCanvas v-if="drawingTool === 'laser'" />
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
    <div v-if="loadingEarlier" class="history-loading">
      正在加载更早的 K 线…
    </div>
    <div v-if="bars.length === 0" class="chart-empty">暂无 K 线数据</div>
  </div>
</template>

<style scoped>
.kline-chart {
  position: relative;
  width: 100%;
  min-width: 0;
  user-select: none;
}
.kline-chart.fill { height: 100%; }
.chart-root {
  width: 100%;
  height: 100%;
}
.risk-drawing-hint { position: absolute; top: 6px; left: 75px; z-index: 18; background: var(--ml-surface); color: var(--ml-text-secondary); padding: 6px; }
.quote-strip {
  position: absolute;
  z-index: 2;
  top: 4px;
  left: 72px;
  right: 42px;
  display: flex;
  flex-wrap: wrap;
  gap: 3px 10px;
  max-height: 34px;
  overflow: hidden;
  color: var(--ml-text-secondary);
  font:
    11px/1.4 ui-monospace,
    Consolas,
    monospace;
  pointer-events: none;
}
.quote-strip span:nth-child(5) {
  color: var(--ml-text-primary);
  font-weight: 700;
}
.compact .quote-strip {
  left: 50px;
  right: 14px;
  gap: 2px 6px;
  max-height: 18px;
  font-size: 9px;
  white-space: nowrap;
}
.quote-panel {
  position: absolute;
  z-index: 2;
  top: 20px;
  left: 66px;
  right: 50px;
  height: 40px;
  pointer-events: none;
  overflow: hidden;
  container-type: inline-size;
  font-family: "SimHei", "Heiti SC", "Microsoft YaHei", sans-serif;
}
.chart-overlay-controls { position:absolute; z-index:4; top:2px; left:66px; right:50px; min-height:18px; pointer-events:none; }
.chart-overlay-controls :slotted(*) { pointer-events:auto; }
.compact .quote-panel {
  left: 48px;
  right: 42px;
  height: 40px;
}
.compact .chart-overlay-controls { left:48px; right:42px; }
.history-loading {
  position: absolute;
  z-index: 5;
  top: 8px;
  right: 8px;
  padding: 4px 8px;
  border: 1px solid var(--ml-divider);
  border-radius: 6px;
  background: color-mix(in srgb, var(--ml-surface) 90%, transparent);
  color: var(--ml-text-secondary);
  font-size: 11px;
  pointer-events: none;
}
.chart-text-input {
  position: absolute;
  z-index: 180;
  min-width: 150px;
  max-width: 280px;
  transform: translateY(-50%);
  padding: 5px 7px;
  border: 1px solid var(--ml-accent);
  border-radius: 3px;
  outline: 2px solid color-mix(in srgb, var(--ml-accent) 22%, transparent);
  background: var(--ml-surface);
  color: var(--ml-text-primary);
  font:
    14px/1.4 "Microsoft YaHei UI",
    "Microsoft YaHei",
    sans-serif;
}
.drawing-active :deep(canvas) {
  cursor: crosshair !important;
}
.cursor-tool .chart-root :deep(canvas) { cursor: crosshair !important; }
.chart-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--ml-text-secondary);
  font-size: 13px;
}
</style>
