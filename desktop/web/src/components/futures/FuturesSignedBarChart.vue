<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";
import { futuresLegend } from "../../domain/futuresChartDesign";
import FuturesLoadingSkeleton from "./FuturesLoadingSkeleton.vue";

export interface SignedPositionRow { key: string; name: string; positive: number | null; negative: number | null }
export interface SignedPositionSummary { positive: number | null; negative: number | null; extent: number }
export interface SignedPositionHover { key: string; direction: "positive" | "negative" | null }
const props = withDefaults(defineProps<{ rows: SignedPositionRow[]; title: string; positiveLabel: string; negativeLabel: string;
  extent: number; selectedKey?: string; summary?: SignedPositionSummary; linkedHoverKey?: string; sharedHover?: boolean;
  showPositive?: boolean; showNegative?: boolean; loading?: boolean }>(), { showPositive: true, showNegative: true });
const emit = defineEmits<{ (event: "select", key: string): void; (event: "hover", value: SignedPositionHover): void }>();
const theme = useThemeStore();
const element = ref<HTMLElement>();
const viewport = ref<HTMLElement>();
const hoveredKey = ref("");
const keyboardRowKey = ref("");
const validRows = computed(() => props.rows);
const hoveredRow = computed(() => validRows.value.find(item => item.key === hoveredKey.value));
const keyboardRow = computed(() => validRows.value.find(item => item.key === keyboardRowKey.value));
const keyboardRowDescription = computed(() => {
  const row = keyboardRow.value;
  if (!row) return "";
  const value = (number: number | null) => number === null ? "未公布" : `${number.toLocaleString("zh-CN")}张`;
  return [row.name, props.showPositive ? `${props.positiveLabel}${value(row.positive)}` : "",
    props.showNegative ? `${props.negativeLabel}${value(row.negative)}` : ""].filter(Boolean).join("，");
});
const activeSelection = computed(() => validRows.value.some(item => item.key === props.selectedKey) ? props.selectedKey : undefined);
const hasValues = computed(() => props.rows.some(item =>
  (props.showPositive !== false && item.positive !== null)
  || (props.showNegative !== false && item.negative !== null)));
const rowHeight = 44;
const visibleRows = 10;
const plotHeight = computed(() => Math.max(visibleRows, validRows.value.length) * rowHeight + 90);
const viewportHeight = visibleRows * rowHeight + 70;
let chart: echarts.ECharts | undefined;
let observer: ResizeObserver | undefined;
let hoveredIndex = -1;
function pairedEmphasis(index: number, action: "highlight" | "downplay"): void {
  if (!chart || index < 0) return;
  for (const id of ["positive-positions", "negative-positions"]) {
    if (id === "positive-positions" && props.showPositive === false) continue;
    if (id === "negative-positions" && props.showNegative === false) continue;
    chart.dispatchAction({ type: action, seriesId: id, dataIndex: index });
  }
}
function clearHover(): void {
  pairedEmphasis(hoveredIndex, "downplay");
  hoveredIndex = -1;
  hoveredKey.value = "";
  emit("hover", { key: "", direction: null });
}
function summaryWidth(value: number | null): string {
  return `${value === null ? 0 : Math.min(50, Math.max(0, value / Math.max(1, props.summary?.extent || 1) * 50))}%`;
}
function summaryValue(value: number | null): string { return value === null ? "未公布" : `${value.toLocaleString("zh-CN")} 张`; }
function focusRow(index: number): void {
  const row = validRows.value[Math.max(0, Math.min(validRows.value.length - 1, index))];
  if (!row) return;
  const position = validRows.value.findIndex(item => item.key === row.key);
  pairedEmphasis(hoveredIndex, "downplay");
  keyboardRowKey.value = row.key;
  hoveredKey.value = row.key;
  hoveredIndex = position;
  pairedEmphasis(position, "highlight");
  emit("hover", { key: row.key, direction: props.showPositive ? "positive" : "negative" });
  const container = viewport.value;
  if (!container) return;
  const top = 70 + position * rowHeight;
  const bottom = top + rowHeight;
  if (top < container.scrollTop) container.scrollTo({ top: Math.max(0, top - 70), behavior: "auto" });
  else if (bottom > container.scrollTop + container.clientHeight)
    container.scrollTo({ top: bottom - container.clientHeight + 8, behavior: "auto" });
}
function beginKeyboardFocus(): void {
  if (!viewport.value?.matches(":focus-visible")) return;
  focusRow(Math.max(0, validRows.value.findIndex(item => item.key === props.selectedKey)));
}
function endKeyboardFocus(): void { keyboardRowKey.value = ""; clearHover(); }
function keyboard(event: KeyboardEvent): void {
  if (!validRows.value.length) return;
  const index = Math.max(0, validRows.value.findIndex(item => item.key === (keyboardRowKey.value || props.selectedKey)));
  let next: number | undefined;
  if (event.key === "ArrowUp") next = index - 1;
  if (event.key === "ArrowDown") next = index + 1;
  if (event.key === "PageUp") next = index - visibleRows;
  if (event.key === "PageDown") next = index + visibleRows;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = validRows.value.length - 1;
  if (next !== undefined) { event.preventDefault(); focusRow(next); }
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    if (keyboardRow.value) emit("select", keyboardRow.value.key);
  }
  if (event.key === "Escape") { event.preventDefault(); endKeyboardFocus(); }
}

function render(): void {
  if (!element.value) return;
  if (!chart) {
    chart = echarts.init(element.value);
    chart.on("click", item => {
      const key = (item.data as { id?: string } | undefined)?.id;
      if (key && validRows.value.some(row => row.key === key)) emit("select", key);
    });
    chart.on("mouseover", item => {
      const key = (item.data as { id?: string } | undefined)?.id;
      const index = validRows.value.findIndex(row => row.key === key);
      if (index < 0) return;
      if (hoveredIndex !== index) pairedEmphasis(hoveredIndex, "downplay");
      hoveredIndex = index;
      hoveredKey.value = key || "";
      pairedEmphasis(index, "highlight");
      emit("hover", { key: hoveredKey.value,
        direction: item.seriesName === props.positiveLabel ? "positive" : "negative" });
    });
    chart.on("mouseout", clearHover);
    chart.on("globalout", clearHover);
  }
  if (!hasValues.value) { clearHover(); chart.clear(); return; }
  if (chart.getHeight() !== element.value.clientHeight) chart.resize();
  const palette = theme.palette;
  const series = [
    { id: "positive-positions", name: props.positiveLabel, type: "bar", stack: "positions", barWidth: 16,
      itemStyle: { color: palette.priceUp, borderRadius: [0, 4, 4, 0] },
      emphasis: { itemStyle: { opacity: 1, borderColor: palette.surface, borderWidth: 1.5,
        shadowBlur: 10, shadowColor: palette.priceUp } },
      data: validRows.value.map(item => ({ value: item.positive, id: item.key,
        itemStyle: item.key === activeSelection.value
          ? { color: palette.priceUp, opacity: 1, borderColor: palette.surface, borderWidth: 1.5,
            shadowBlur: 14, shadowColor: palette.priceUp, shadowOffsetY: 1 }
          : item.key === props.linkedHoverKey
            ? { opacity: 1, borderColor: palette.accent, borderWidth: 1, shadowBlur: 5, shadowColor: palette.accentSoft }
          : { opacity: activeSelection.value ? .55 : .9 } })) },
    { id: "negative-positions", name: props.negativeLabel, type: "bar", stack: "positions", barWidth: 16,
      itemStyle: { color: palette.priceDown, borderRadius: [4, 0, 0, 4] },
      emphasis: { itemStyle: { opacity: 1, borderColor: palette.surface, borderWidth: 1.5,
        shadowBlur: 10, shadowColor: palette.priceDown } },
      data: validRows.value.map(item => ({ value: item.negative === null ? null : -item.negative, id: item.key,
        itemStyle: item.key === activeSelection.value
          ? { color: palette.priceDown, opacity: 1, borderColor: palette.surface, borderWidth: 1.5,
            shadowBlur: 14, shadowColor: palette.priceDown, shadowOffsetY: 1 }
          : item.key === props.linkedHoverKey
            ? { opacity: 1, borderColor: palette.accent, borderWidth: 1, shadowBlur: 5, shadowColor: palette.accentSoft }
          : { opacity: activeSelection.value ? .55 : .9 } })) },
  ].filter((_, index) => index === 0 ? props.showPositive !== false : props.showNegative !== false);
  chart.setOption({
    animation: true, animationDuration: 220,
    animationDurationUpdate: 120, animationEasingUpdate: "cubicOut",
    stateAnimation: { duration: 100, easing: "cubicOut" },
    backgroundColor: "transparent", aria: { enabled: true },
    tooltip: { show: false },
    legend: { ...futuresLegend(palette), data: series.map(item => item.name) },
    grid: { left: 128, right: 36, top: 70, bottom: 20 },
    xAxis: { type: "value", position: "top", name: "持仓量（张）",
      min: props.showNegative === false ? 0 : -Math.max(1, props.extent),
      max: props.showPositive === false ? 0 : Math.max(1, props.extent),
      axisLabel: { color: palette.chartAxis, formatter: (value: number) => value.toLocaleString("zh-CN") },
      nameTextStyle: { color: palette.chartAxis }, splitLine: { lineStyle: { color: palette.chartGrid } } },
    yAxis: { type: "category", inverse: true, data: validRows.value.map(item => item.name),
      axisLabel: { interval: 0, color: palette.chartAxis, width: 112, overflow: "truncate", fontSize: 11,
        formatter: (name: string, index: number) => validRows.value[index]?.key === activeSelection.value ? `{active|${name}}` : name,
        rich: { active: { color: palette.accent, fontWeight: 700, backgroundColor: palette.accentSoft,
          borderColor: palette.accent, borderWidth: 1, borderRadius: 6, padding: [4, 7] } } },
      axisLine: { lineStyle: { color: palette.chartGrid } },
      splitLine: { show: true, lineStyle: { color: palette.chartGrid, opacity: .45 } } },
    series,
  });
  if (keyboardRowKey.value) {
    const index = validRows.value.findIndex(row => row.key === keyboardRowKey.value);
    if (index >= 0) pairedEmphasis(index, "highlight");
  }
}

watch(() => [props.rows, props.extent, props.selectedKey, props.linkedHoverKey, theme.palette], () => void nextTick(render), { deep: true });
watch(() => props.selectedKey, key => { if (!key) return; void nextTick(() => {
  const index = validRows.value.findIndex(row => row.key === key);
  if (index >= 0) viewport.value?.scrollTo({ top: Math.max(0, index * rowHeight - 3 * rowHeight), behavior: "smooth" });
}); });
onMounted(() => {
  void nextTick(render);
  observer = new ResizeObserver(() => chart?.resize());
  if (element.value) observer.observe(element.value);
});
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose(); });
</script>

<template>
  <article class="signed-bar-panel">
    <header class="bar-heading"><h3>{{ title }}</h3><slot name="controls" /></header>
    <div v-if="!sharedHover" class="hover-values" :class="{ visible: hoveredRow }" data-test="signed-bar-hover-values" :data-hover-key="hoveredRow?.key || ''">
      <template v-if="hoveredRow"><strong>{{ hoveredRow.name }}</strong><span class="positive">{{ positiveLabel }} {{ hoveredRow.positive == null ? '未公布' : `${hoveredRow.positive.toLocaleString('zh-CN')} 张` }}</span><span class="negative">{{ negativeLabel }} {{ hoveredRow.negative == null ? '未公布' : `${hoveredRow.negative.toLocaleString('zh-CN')} 张` }}</span></template>
    </div>
    <button v-if="summary && (!loading || hasValues)" class="summary-bar" :class="{ active: !selectedKey }" type="button" data-test="futures-summary-bar"
      :data-positive="summary.positive ?? ''" :data-negative="summary.negative ?? ''"
      :aria-label="`全部席位：${positiveLabel}${summaryValue(summary.positive)}，${negativeLabel}${summaryValue(summary.negative)}；点击查看全部席位历史`"
      @click="emit('select', '')">
      <span class="summary-heading"><strong>全部席位</strong><span class="positive">{{ positiveLabel }} {{ summaryValue(summary.positive) }}</span><span class="negative">{{ negativeLabel }} {{ summaryValue(summary.negative) }}</span></span>
      <span class="summary-track"><span class="summary-negative" :style="{ width: summaryWidth(summary.negative) }" /><span class="summary-zero" /><span class="summary-positive" :style="{ width: summaryWidth(summary.positive) }" /></span>
      <span class="summary-scale"><span>−{{ summary.extent.toLocaleString('zh-CN') }}</span><span>0</span><span>+{{ summary.extent.toLocaleString('zh-CN') }} 张</span></span>
    </button>
    <div ref="viewport" class="bar-scroll" tabindex="0" role="region" :aria-label="`${title}，方向键定位成员，回车查看历史`" :style="{ height: `${viewportHeight}px` }" data-test="futures-signed-scroll" @focus="beginKeyboardFocus" @blur="endKeyboardFocus" @keydown="keyboard">
      <span class="keyboard-reading" aria-live="polite" aria-atomic="true">{{ keyboardRowDescription }}</span>
      <div ref="element" :style="{ height: `${plotHeight}px`, width: '100%' }" data-test="futures-signed-bar"
        :data-row-count="validRows.length" :data-row-keys="validRows.map(item => item.key).join(',')"
        :data-selected-key="selectedKey || ''" :data-selection-effect="activeSelection ? 'glow-and-label' : 'none'"
        :data-linked-hover-key="linkedHoverKey || ''"
        :data-visible-count="visibleRows" data-orientation="horizontal" data-scroll-direction="vertical"
        :data-positive="validRows.map(item => item.positive ?? '').join(',')"
        :data-negative="validRows.map(item => item.negative === null ? '' : -item.negative).join(',')"
        role="img" :aria-label="title" />
      <div v-if="loading && !hasValues" class="bar-loading-skeleton"><FuturesLoadingSkeleton kind="ranking" /></div>
    </div>
    <div v-if="!loading && !hasValues" class="empty">该日没有可显示持仓</div>
  </article>
</template>

<style scoped>
.signed-bar-panel { position: relative; min-width: 0; padding: 14px 12px 12px; border: 1px solid var(--ml-divider); border-radius: 10px; background: var(--ml-surface-elevated); }
.signed-bar-panel:focus-within { border-color: color-mix(in srgb,var(--ml-accent) 45%,var(--ml-divider)); }
h3 { font-size: 13px; font-weight: 600; margin: 2px 0 10px; }
.bar-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.bar-heading h3 { margin: 5px 0; }
.hover-values { min-height: 30px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; margin: -5px 0 5px; padding: 3px 8px; border-radius: 8px; color: var(--ml-text-secondary); font-size: 11px; opacity: 0; transition: opacity 120ms; pointer-events: none; }
.hover-values.visible { opacity: 1; background: var(--ml-surface-selected); }
.hover-values strong { color: var(--ml-text-primary); font-size: 12px; }
.hover-values .positive { color: var(--ml-price-up); }
.hover-values .negative { color: var(--ml-price-down); }
.summary-bar { display: block; width: 100%; margin: 2px 0 8px; padding: 9px 10px; border: 1px solid var(--ml-divider); border-radius: 8px; background: var(--ml-surface); color: var(--ml-text-primary); text-align: left; cursor: pointer; transition: border-color 160ms, background-color 160ms; }
.summary-bar:hover,.summary-bar.active { border-color: color-mix(in srgb,var(--ml-accent) 50%,var(--ml-divider)); background: color-mix(in srgb,var(--ml-accent) 4%,var(--ml-surface)); }
.summary-bar:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
.summary-heading,.summary-scale { display: flex; justify-content: space-between; gap: 8px; align-items: center; white-space: nowrap; font-size: 11px; }
.summary-heading { flex-wrap: wrap; }
.summary-heading strong { color: var(--ml-text-primary); font-size: 12px; }
.summary-heading .positive { color: var(--ml-price-up); }.summary-heading .negative { color: var(--ml-price-down); }
.summary-track { display: block; position: relative; height: 16px; margin: 7px 0 3px; border-radius: 5px; background: var(--ml-surface-selected); overflow: hidden; }
.summary-positive,.summary-negative { position: absolute; top: 1px; height: 14px; transition: width 120ms cubic-bezier(.2,.7,.2,1); }
.summary-positive { left: 50%; border-radius: 0 4px 4px 0; background: var(--ml-price-up); }
.summary-negative { right: 50%; border-radius: 4px 0 0 4px; background: var(--ml-price-down); }
.summary-zero { position: absolute; z-index: 1; top: 0; bottom: 0; left: 50%; width: 1px; background: var(--ml-chart-axis); }
.summary-scale { color: var(--ml-text-secondary); font-variant-numeric: tabular-nums; }
.bar-scroll { position: relative; max-width: 100%; overflow-y: auto; overflow-x: hidden; scrollbar-width: thin; scrollbar-color: var(--ml-chart-secondary) transparent; border-radius: 6px; }
.bar-scroll:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
.keyboard-reading { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.bar-loading-skeleton { position: absolute; inset: 0; z-index: 1; padding: 4px 8px; background: var(--ml-surface-elevated); }
.bar-scroll::-webkit-scrollbar { width: 6px; }.bar-scroll::-webkit-scrollbar-thumb { background: var(--ml-chart-secondary); border-radius: 6px; }.bar-scroll::-webkit-scrollbar-track { background: transparent; }
.empty { position: absolute; inset: 55px 0 0; display: grid; place-items: center; pointer-events: none; color: var(--ml-text-secondary); }
@media (prefers-reduced-motion: reduce) { .summary-positive,.summary-negative,.summary-bar,.hover-values { transition: none; } }
</style>
