<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { FuturesPairedBubblePayload } from "../../domain/futures";
import { bubbleColorsFor, minimumBubbleColorDistance, minimumBubbleTextContrast } from "../../domain/futuresBubbleColors";
import { depositBubbleDiameter, fluidBubbleValue, fluidBubbleVisibility, resolveBubbleFrames } from "../../domain/futuresBubbleMotion";
import { turnoverAmount, turnoverAxis, turnoverPosition } from "../../domain/futuresTurnoverAxis";
import { DIRECTION_AXIS_REFERENCE_PP, directionAmount, directionPosition } from "../../domain/futuresDirectionAxis";
import { useThemeStore } from "../../stores/theme";
import { futuresTooltip } from "../../domain/futuresChartDesign";

type ViewBounds = { xmin: number; xmax: number; ymin: number; ymax: number };
const props = defineProps<{ payload: FuturesPairedBubblePayload; position: number; selectedKeys: string[]; search: string }>();
const emit = defineEmits<{ (event: "select", key: string): void }>();
const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;
const viewBounds = ref<ViewBounds>();
const dragging = ref(false);
let drag: { pointer: number; x: number; y: number; view: ViewBounds } | undefined;
let suppressClickUntil = 0;

function robustLimit(values: number[], minimum = 1): number {
  const ordered = values.filter(Number.isFinite).map(Math.abs).sort((a, b) => a - b);
  return ordered.length ? Math.max(minimum, ordered[Math.floor((ordered.length - 1) * .99)] * 1.08) : minimum;
}

const xScale = computed(() => turnoverAxis(props.payload));
const bounds = computed(() => {
  const xs: number[] = [], ys: number[] = [], sizes: number[] = [];
  for (const item of props.payload.series) {
    for (const value of item.horizontalValues) if (typeof value === "number" && Number.isFinite(value) && value >= 0)
      xs.push(turnoverPosition(xScale.value, value));
    for (const value of item.verticalValues) if (typeof value === "number" && Number.isFinite(value)) ys.push(directionPosition(value));
    for (const value of item.sizeValues) if (typeof value === "number" && Number.isFinite(value)) sizes.push(value);
  }
  const x = xScale.value.mode === "quantile" ? xs.reduce((maximum, value) => Math.max(maximum, value), 1) * 1.08 : robustLimit(xs);
  // The signed accumulated direction is a real value, not a rank. Include
  // every observation and reserve enough room for the bubble-radius inset.
  const y = ys.reduce((maximum, value) => Math.max(maximum, Math.abs(value)), .1);
  const size = robustLimit(sizes);
  return { xmin: 0, xmax: 1.12 * x,
    ymin: props.payload.ySigned ? -1.35 * y : -.15 * y, ymax: 1.35 * y, size };
});
const colors = computed(() => bubbleColorsFor(props.payload.series.map(item => item.key)));
const colorDistance = computed(() => minimumBubbleColorDistance(Object.values(colors.value)));
const textContrast = computed(() => minimumBubbleTextContrast(Object.values(colors.value)));
const xLabel = computed(() => props.payload.xLabel);
const yLabel = computed(() => props.payload.yLabel);
const yTitle = computed(() => (yLabel.value.match(/\d+|亿元|百分点|（%）|./gu) || []).join("\n"));
const framePosition = computed(() => Math.max(0, Math.min(Math.max(0, props.payload.dates.length - 1), props.position)));
const currentDay = computed(() => props.payload.dates[Math.round(framePosition.value)] || "");
const frames = computed(() => props.payload.series.map(item => resolveBubbleFrames(item, props.payload.dates)));

const states = computed(() => {
  const left = Math.floor(framePosition.value), right = Math.min(left + 1, props.payload.dates.length - 1);
  const progress = framePosition.value - left;
  return props.payload.series.map((item, index) => {
    const productFrames = frames.value[index];
    const before = productFrames[left], after = productFrames[right];
    const previous = productFrames[left - 1], following = productFrames[right + 1];
    const source = progress < .5 ? before || after : after || before;
    const sourceIndex = progress < .5 ? before ? left : right : after ? right : left;
    let x = 0, y = 0, size = 0, visibility = 0;
    if (before && after) {
      x = fluidBubbleValue(previous?.x, before.x, after.x, following?.x, progress);
      y = fluidBubbleValue(previous?.y, before.y, after.y, following?.y, progress);
      size = fluidBubbleValue(previous?.size, before.size, after.size, following?.size, progress);
      visibility = 1;
    } else if (before) {
      ({ x, y, size } = before); visibility = 1;
    } else if (after) {
      ({ x, y } = after); visibility = fluidBubbleVisibility(progress); size = after.size * visibility;
    }
    return { key: item.key, name: item.name, x, y, size, visibility, source: source?.source,
      sourceDay: props.payload.dates[sourceIndex] || currentDay.value,
      heldXDay: source?.heldXDay, heldYDay: source?.heldYDay, heldSizeDay: source?.heldSizeDay,
      missing: source?.missing || false,
      transitioning: progress > .001 && progress < .999 };
  });
});
const pointCount = computed(() => frames.value.filter(productFrames => productFrames[Math.round(framePosition.value)]).length);
const incompleteCount = computed(() => frames.value.filter(productFrames => productFrames[Math.round(framePosition.value)]?.missing).length);
const visibleCount = computed(() => states.value.filter(item => item.visibility > .01).length);

function escape(value: string): string { return value.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char] || char)); }
function dataForFrame() {
  const palette = theme.palette, query = props.search.trim().toLocaleLowerCase();
  return states.value.map(item => {
    const selected = props.selectedKeys.includes(item.key);
    const matched = !query || `${item.key} ${item.name}`.toLocaleLowerCase().includes(query);
    const zeroActivity = item.source?.x === 0 && item.source?.size === 0 && item.source?.y !== null;
    const hollow = item.missing;
    const diameter = depositBubbleDiameter(item.size, bounds.value.size);
    const plottedX = turnoverPosition(xScale.value, item.x);
    const xDistance = plottedX / Math.max(1, bounds.value.xmax);
    const plottedY = directionPosition(item.y);
    const yDistance = plottedY / Math.max(1, Math.max(Math.abs(bounds.value.ymin), bounds.value.ymax));
    const prominence = Math.min(1, Math.hypot(xDistance, yDistance));
    const baseOpacity = props.selectedKeys.length ? selected ? 1 : .035 : matched ? .22 + .73 * prominence : .1;
    const opacity = (hollow || item.size === 0) && matched && !props.selectedKeys.length
      ? Math.max(.6, baseOpacity) : baseOpacity;
    const yInset = (bounds.value.ymax - bounds.value.ymin) * .1;
    const visibleX = Math.min(bounds.value.xmax - (bounds.value.xmax - bounds.value.xmin) * .08, plottedX);
    const visibleY = Math.max(bounds.value.ymin + yInset, Math.min(bounds.value.ymax - yInset, plottedY));
    const view = viewBounds.value || bounds.value;
    const insideView = visibleX >= view.xmin && visibleX <= view.xmax && visibleY >= view.ymin && visibleY <= view.ymax;
    return { id: item.key, ...item, hollow, zeroActivity,
      value: [visibleX, visibleY, item.size],
      symbolSize: insideView ? diameter * Math.sqrt(item.visibility) : 0, symbol: "circle", prominence,
      itemStyle: { color: hollow ? "transparent" : colors.value[item.key],
        opacity: item.visibility * opacity,
        borderColor: selected ? palette.accent : colors.value[item.key], borderWidth: selected ? 3 : hollow ? 2 : 1 },
      emphasis: props.selectedKeys.length ? { focus: "none", itemStyle: { opacity: selected ? 1 : .09 } } : undefined,
      label: { show: false, formatter: item.name, color: palette.textPrimary, fontSize: 11 },
    };
  });
}

type ShortSignal = { shortMove3?: number | null; shortRank?: number | null; shortSampleCount?: number | null; shortCompareStatus?: string };

function shortDirection(source: ShortSignal | undefined): "up" | "down" | null {
  if (source?.shortCompareStatus !== "COMPLETE" || typeof source.shortMove3 !== "number"
      || typeof source.shortRank !== "number" || !Number.isInteger(source.shortSampleCount)
      || (source.shortSampleCount ?? 0) < 20 || !Number.isFinite(source.shortMove3)
      || !Number.isFinite(source.shortRank) || source.shortRank < 0 || source.shortRank > 1) return null;
  if (source.shortMove3 > 0 && source.shortRank >= .9) return "up";
  if (source.shortMove3 < 0 && source.shortRank <= .1) return "down";
  return null;
}

function shortHighlightData() {
  return dataForFrame().flatMap(item => {
    const direction = shortDirection(item.source as (typeof item.source & ShortSignal));
    const query = props.search.trim().toLocaleLowerCase();
    if (!direction || item.missing || item.visibility <= .01 || item.symbolSize <= 0
        || (props.selectedKeys.length > 0 && !props.selectedKeys.includes(item.key))
        || (query && !`${item.key} ${item.name}`.toLocaleLowerCase().includes(query))) return [];
    const color = direction === "up" ? "#e04e54" : "#10a77d";
    return [{ id: item.key, value: item.value, symbolSize: item.symbolSize + 9,
      itemStyle: { color: "transparent", borderColor: color, borderWidth: 2.5, opacity: item.visibility },
      label: { show: true, formatter: `${direction === "up" ? "↑" : "↓"} ${item.name}`,
        position: "top", color, fontWeight: 700, fontSize: 11 } }];
  });
}

function renderFrame(): void {
  if (!chart) return;
  chart.setOption({ series: [{ id: "bubble-products", data: dataForFrame() },
    { id: "short-highlight", data: shortHighlightData() }],
    graphic: [{ id: "date-watermark", style: { text: currentDay.value } }] }, { lazyUpdate: true });
}

function applyView(view: ViewBounds): void {
  const offset = Math.max(0, -view.xmin);
  const safeView = { ...view, xmin: view.xmin + offset, xmax: view.xmax + offset };
  viewBounds.value = safeView;
  chart?.setOption({ xAxis: { min: safeView.xmin, max: safeView.xmax }, yAxis: { min: safeView.ymin, max: safeView.ymax },
    series: [{ id: "bubble-products", data: dataForFrame() }, { id: "short-highlight", data: shortHighlightData() }] }, { lazyUpdate: true });
}

function beginPan(event: PointerEvent): void {
  if (event.button !== 0 || !element.value) return;
  drag = { pointer: event.pointerId, x: event.clientX, y: event.clientY, view: { ...(viewBounds.value || bounds.value) } };
}

function pan(event: PointerEvent): void {
  if (!drag || drag.pointer !== event.pointerId || !element.value) return;
  const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
  if (!dragging.value && Math.hypot(dx, dy) < 4) return;
  dragging.value = true;
  if (!element.value.hasPointerCapture(event.pointerId)) element.value.setPointerCapture(event.pointerId);
  event.preventDefault();
  const rectangle = element.value.getBoundingClientRect();
  const shiftX = -dx / Math.max(1, rectangle.width - 100) * (drag.view.xmax - drag.view.xmin);
  const shiftY = dy / Math.max(1, rectangle.height - 100) * (drag.view.ymax - drag.view.ymin);
  applyView({ xmin: drag.view.xmin + shiftX, xmax: drag.view.xmax + shiftX,
    ymin: drag.view.ymin + shiftY, ymax: drag.view.ymax + shiftY });
}

function endPan(event: PointerEvent): void {
  if (dragging.value) suppressClickUntil = performance.now() + 250;
  if (element.value?.hasPointerCapture(event.pointerId)) element.value.releasePointerCapture(event.pointerId);
  drag = undefined; dragging.value = false;
}

function zoom(event: WheelEvent): void {
  if (!element.value) return;
  const view = viewBounds.value || bounds.value, rectangle = element.value.getBoundingClientRect();
  const ratioX = Math.max(0, Math.min(1, (event.clientX - rectangle.left - 76) / Math.max(1, rectangle.width - 100)));
  const ratioY = Math.max(0, Math.min(1, (event.clientY - rectangle.top - 35) / Math.max(1, rectangle.height - 100)));
  const factor = Math.exp(Math.max(-1, Math.min(1, event.deltaY * .001)));
  const width = Math.max((bounds.value.xmax - bounds.value.xmin) * .001, Math.min((bounds.value.xmax - bounds.value.xmin) * 20, (view.xmax - view.xmin) * factor));
  const height = Math.max((bounds.value.ymax - bounds.value.ymin) * .001, Math.min((bounds.value.ymax - bounds.value.ymin) * 20, (view.ymax - view.ymin) * factor));
  const anchorX = view.xmin + ratioX * (view.xmax - view.xmin), anchorY = view.ymax - ratioY * (view.ymax - view.ymin);
  applyView({ xmin: anchorX - ratioX * width, xmax: anchorX + (1 - ratioX) * width,
    ymin: anchorY - (1 - ratioY) * height, ymax: anchorY + ratioY * height });
}

function resetView(): void { applyView({ ...bounds.value }); }

function axisNumber(value: number, extent: number, signed = false): string {
  if (!Number.isFinite(value)) return "";
  if (!signed && value < 0) return "";
  if (Math.abs(value) >= 1e8) return `${Number((value / 1e8).toFixed(1))}亿`;
  if (Math.abs(value) >= 1e4) return `${Number((value / 1e4).toFixed(1))}万`;
  return Number(value.toFixed(extent <= 2 ? 2 : extent <= 10 ? 1 : 0)).toLocaleString("zh-CN", { signDisplay: signed ? "exceptZero" : "auto" });
}

function turnoverAxisNumber(position: number, extent: number): string {
  const amount = turnoverAmount(xScale.value, position);
  if (xScale.value.mode === "linear" || amount < 0 || amount >= 10_000)
    return axisNumber(amount, extent);
  if (!Number.isFinite(amount)) return "";
  return amount.toLocaleString("zh-CN", { maximumFractionDigits: amount < .01 ? 6 : amount < 1 ? 4 : 2 });
}

function displayValue(value: number | null, unit: "张" | "亿元" | "%" | "百分点"): string {
  if (value === null) return "当日缺报";
  const prefix = value > 0 && (unit === "%" || unit === "百分点") ? "+" : "";
  return `${prefix}${value.toLocaleString("zh-CN", { minimumFractionDigits: unit === "张" ? 0 : 2, maximumFractionDigits: unit === "张" ? 0 : 2 })}${unit}`;
}

function configure(): void {
  if (!element.value) return;
  if (!chart) {
    chart = echarts.init(element.value);
    chart.on("click", event => { const item = event.data as { key?: string; visibility?: number }; if (performance.now() >= suppressClickUntil && item.key && item.visibility) emit("select", item.key); });
  }
  const palette = theme.palette;
  const view = viewBounds.value || bounds.value;
  chart.setOption({
    animation: false, backgroundColor: "transparent", aria: { enabled: true, label: {
      description: "品种气泡分布图：横轴为当日成交额，纵轴为近60个交易日的方向累计，气泡大小表示沉淀资金；悬浮可查看原始数值和缺报原因。",
    } },
    tooltip: { ...futuresTooltip(palette), trigger: "item", formatter: (params: unknown) => {
        const item = (params as { data: ReturnType<typeof dataForFrame>[number] }).data;
        if (!item.source) return "";
        const raw = item.source as typeof item.source & ShortSignal;
        const row = (label: string, value: string) => `<div style="display:flex;justify-content:space-between;gap:22px"><span style="color:${palette.textSecondary}">${label}</span><strong>${value}</strong></div>`;
        const carried = [
          raw.x === null ? item.heldXDay ? `成交额位置沿用 ${item.heldXDay}` : "成交额缺值，暂置于零轴" : "",
          raw.y === null ? item.heldYDay ? `方向累计位置沿用 ${item.heldYDay}` : "方向累计缺值，暂置于零轴" : "",
          raw.size === null ? item.heldSizeDay ? `气泡大小沿用 ${item.heldSizeDay}` : "沉淀资金缺值，大小暂用最小空心圆" : "",
        ].filter(Boolean).join("；");
        return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:7px"><span style="width:9px;height:9px;border-radius:50%;background:${colors.value[item.key]}"></span><strong>${escape(item.name)}</strong><span style="margin-left:auto;color:${palette.textSecondary}">${escape(item.sourceDay)}</span></div>`
          + row(escape(xLabel.value), displayValue(raw.x, props.payload.xUnit))
          + row(escape(yLabel.value), displayValue(raw.y, props.payload.yUnit))
          + row(escape(props.payload.sizeLabel), displayValue(raw.size, props.payload.sizeUnit))
          + row("当日持仓加权涨跌幅", displayValue(raw.dailyReturn, "%"))
          + row("近1～10交易日加权结算价首尾变化", displayValue(raw.tenDayReturn, "%"))
          + (raw.shortCompareStatus === "COMPLETE" && typeof raw.shortMove3 === "number"
            && Number.isFinite(raw.shortMove3) && Number.isInteger(raw.shortSampleCount)
            && (raw.shortSampleCount ?? 0) >= 20 && typeof raw.shortRank === "number"
            && Number.isFinite(raw.shortRank) && raw.shortRank >= 0 && raw.shortRank <= 1
            ? row("近3日累计及相对强弱", `${displayValue(raw.shortMove3, "百分点")} · ${(raw.shortRank * 100).toFixed(1)}%${typeof raw.shortSampleCount === "number" ? `（${raw.shortSampleCount}品种）` : ""}`)
            : "")
          + (raw.directionWindowCount === null ? "" : row("方向累计有效交易日",
            `${raw.directionWindowCount}日${raw.directionStatus === "SHORT" ? "（不足60日）" : ""}`))
          + (raw.directionUpCount === null || raw.directionDownCount === null || raw.directionFlatCount === null ? ""
            : row("上涨／下跌／平盘", `${raw.directionUpCount}／${raw.directionDownCount}／${raw.directionFlatCount}日`))
          + (raw.coverage === undefined ? "" : row("加权结算价窗口覆盖",
            `${(raw.coverage * 100).toFixed(raw.coverage < 1 && raw.coverage > .999 ? 4 : 2)}%`))
          + (raw.windowMin === undefined ? "" : row("品种加权价格窗口",
            `${raw.windowMin}${raw.windowMax !== raw.windowMin ? `～${raw.windowMax}` : ""}个交易日`))
          + (!raw.marginPublicationDay ? "" : row("中金所保证金公布日",
            `${escape(raw.marginPublicationDay)}${raw.marginPublicationDay < item.sourceDay ? "（沿用最近公布）" : ""}`))
          + (raw.size === null && raw.depositReason ? `<div style="color:${palette.textSecondary};font-size:11px;margin-top:5px">资金缺报原因：${escape(raw.depositReason)}${raw.missingMarginContracts === undefined ? "" : `；缺保证金率合约 ${raw.missingMarginContracts} 个`}</div>` : "")
          + (raw.y === null && raw.directionReason ? `<div style="color:${palette.textSecondary};font-size:11px;margin-top:5px">方向累计缺报：${escape(raw.directionReason)}</div>` : "")
          + (item.hollow ? `<div style="color:${palette.textSecondary};font-size:11px;margin-top:5px">空心表示三项中有缺报；${escape(carried)}</div>` : "")
          + (item.zeroActivity ? `<div style="color:${palette.textSecondary};font-size:11px;margin-top:5px">当日成交额与沉淀资金均为真实零值，方向累计有值，按最小实心圆显示</div>` : "")
          + `<div style="color:${palette.textSecondary};font-size:11px;margin-top:5px">方向累计将各日持仓加权涨跌幅直接相加；10日变化取加权结算价首尾，两者口径不同。</div>`
          + (!item.missing && raw.size === 0 && !item.zeroActivity ? `<div style="color:${palette.textSecondary};font-size:11px;margin-top:5px">沉淀资金为真实零值，按最小实心圆显示</div>` : "")
          + (item.transitioning ? `<div style="color:${palette.textDisabled};font-size:11px;margin-top:5px">过渡中 · 提示为最近原件日</div>` : '');
      } },
    graphic: [{ id: "date-watermark", type: "text", right: 48, top: 7, silent: true,
      style: { text: currentDay.value, fill: palette.textDisabled, opacity: .5, fontSize: 14, fontFamily: "ui-monospace, Consolas, monospace" } }],
    grid: { left: 76, right: 24, top: 35, bottom: 65 },
    xAxis: { type: "value", splitNumber: 6, name: xLabel.value, nameLocation: "middle", nameGap: 32,
      min: view.xmin, max: view.xmax, axisLabel: { color: palette.chartAxis,
        formatter: (value: number) => turnoverAxisNumber(value, view.xmax - view.xmin) },
      nameTextStyle: { color: palette.chartAxis }, splitLine: { lineStyle: { color: palette.chartGrid } }, axisLine: { onZero: true, lineStyle: { color: palette.chartAxis } } },
    yAxis: { type: "value", splitNumber: 6, name: yTitle.value, nameLocation: "middle", nameRotate: 0, nameGap: 52,
      min: view.ymin, max: view.ymax, axisLabel: { color: palette.chartAxis,
        formatter: (value: number) => axisNumber(directionAmount(value),
          Math.abs(directionAmount(view.ymax) - directionAmount(view.ymin)), props.payload.ySigned) },
      nameTextStyle: { color: palette.chartAxis, lineHeight: 13, fontSize: 11, align: "center" }, splitLine: { lineStyle: { color: palette.chartGrid, type: "dashed" } }, axisLine: { onZero: true, lineStyle: { color: palette.chartAxis } } },
    series: [{ id: "bubble-products", name: "品种", type: "scatter", clip: false, data: dataForFrame(),
      emphasis: { focus: "self", scale: 1.12, label: { show: true, position: "top", color: palette.textPrimary,
        backgroundColor: palette.chartTooltip, padding: [5, 8], borderRadius: 7, fontWeight: 700 },
        itemStyle: { opacity: 1, borderColor: palette.accent, borderWidth: 2, shadowBlur: 12, shadowColor: palette.accentSoft } },
      blur: { itemStyle: { opacity: .12 } },
      markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: palette.chartAxis, type: "solid", opacity: .5 }, data: [{ xAxis: turnoverPosition(xScale.value, 0) }, { yAxis: 0 }] } },
    { id: "short-highlight", name: "近3日相对强弱", type: "scatter", clip: false, data: shortHighlightData(),
      silent: true, z: 5, symbol: "circle", labelLayout: { hideOverlap: true },
      tooltip: { show: false }, emphasis: { disabled: true } }],
  }, true);
}

watch(() => props.payload, () => { viewBounds.value = undefined; void nextTick(configure); }, { deep: true });
watch(() => theme.palette, () => void nextTick(configure), { deep: true });
watch(() => [props.position, props.selectedKeys, props.search], renderFrame, { deep: true });
onMounted(() => { void nextTick(configure); observer = new ResizeObserver(() => { chart?.resize(); renderFrame(); }); if (element.value) observer.observe(element.value); });
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose(); });
</script>

<template>
  <div class="bubble-wrap">
    <div ref="element" class="bubble" :class="{ dragging }" data-test="futures-bubble" :data-point-count="pointCount" :data-incomplete-count="incompleteCount" :data-position="position"
      :data-scale="JSON.stringify(bounds)" :data-colors="JSON.stringify(colors)" :data-min-color-distance="colorDistance"
      :data-x-scale="JSON.stringify(xScale)"
      :data-y-reference="DIRECTION_AXIS_REFERENCE_PP"
      :data-short-highlight-count="shortHighlightData().length"
      :data-min-text-contrast="textContrast" :data-label-count="0" :data-view="JSON.stringify(viewBounds || bounds)"
      :data-axis-x="xLabel" :data-axis-y="yLabel" :data-axis-y-title="yTitle" role="img" aria-label="全品种气泡分布"
      @pointerdown="beginPan" @pointermove="pan" @pointerup="endPan" @pointercancel="endPan" @wheel.prevent="zoom" @dblclick="resetView" />
    <p class="axis-note" data-test="futures-bubble-axis-note" v-if="xScale.mode === 'quantile'">横轴按 {{ xScale.referenceDay }} 的 {{ xScale.sampleCount }} 个有效品种成交额分位展开；刻度显示真实金额，播放期间刻度固定。相同金额位于同一位置，缺报值不参与分位计算。</p>
    <p class="axis-note">纵轴使用固定非线性刻度展开较小方向变化；刻度及悬浮显示实际累计百分点，播放时同值位置不变。</p>
    <div v-if="!visibleCount" class="empty">该日缺少气泡所需的区间数据</div>
    <button class="bubble-reset" type="button" aria-label="复位气泡视图" title="复位视图（双击图表也可复位）" @click="resetView">↺</button>
  </div>
</template>

<style scoped>
.bubble-wrap { position: relative; min-width: 0; }
.bubble { width: 100%; max-width: 1120px; height: clamp(680px, 78vh, 860px); margin-inline: auto; cursor: grab; touch-action: none; user-select: none; }
.bubble.dragging { cursor: grabbing; }
.empty { position: absolute; inset: 0; display: grid; place-items: center; pointer-events: none; color: var(--ml-text-secondary); }
.bubble-reset { position: absolute; top: 2px; right: 4px; width: 30px; height: 30px; display: grid; place-items: center; border: 1px solid var(--ml-divider); border-radius: 9px; background: var(--ml-surface-elevated); color: var(--ml-text-secondary); font-size: 19px; cursor: pointer; transition: color 160ms, border-color 160ms, box-shadow 160ms; }
.bubble-reset:hover, .bubble-reset:focus-visible { color: var(--ml-accent); border-color: var(--ml-accent); box-shadow: 0 0 0 3px var(--ml-accent-soft); }
.axis-note { margin: 4px 0 0; color: var(--ml-text-secondary); font-size: 12px; line-height: 1.5; text-align: center; }
@media (max-width: 900px) { .bubble { height: clamp(440px, 68vh, 620px); } }
</style>
