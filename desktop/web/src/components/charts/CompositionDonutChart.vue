<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";

export interface CompositionItem {
  name: string;
  value: number;
  color?: string;
}

const props = withDefaults(defineProps<{
  title?: string;
  items: CompositionItem[];
  height?: number;
}>(), { title: "", items: () => [], height: 220 });

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;

const validItems = computed(() => props.items.filter(item => Number.isFinite(item.value) && item.value >= 0));
const visibleItems = computed(() => validItems.value.filter(item => item.value > 0));
const total = computed(() => visibleItems.value.reduce((sum, item) => sum + item.value, 0));
const hasData = computed(() => Number.isFinite(total.value) && total.value > 0);
const accessibleSummary = computed(() => {
  if (!hasData.value) return `${props.title || "构成图"}：暂无可计算的数据`;
  const parts = validItems.value.map(item =>
    `${item.name} ${item.value.toLocaleString("zh-CN")}，占比 ${((item.value / total.value) * 100).toFixed(1)}%`,
  );
  return `${props.title || "构成图"}：${parts.join("；")}`;
});

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char] || char));
}

function render(): void {
  if (!element.value) return;
  if (!hasData.value) { chart?.clear(); return; }
  chart ??= echarts.init(element.value);
  const palette = theme.palette;
  const colors = [palette.priceUp, palette.priceDown, palette.flat, palette.accent, palette.highlight, palette.chartSecondary];
  chart.setOption({
    backgroundColor: "transparent",
    animationDuration: 250,
    aria: { enabled: true, decal: { show: false } },
    tooltip: {
      trigger: "item",
      formatter: (item: { name: string; value: number; percent: number }) =>
        `${escapeHtml(item.name)}：${item.value.toLocaleString("zh-CN")}（${item.percent.toFixed(1)}%）`,
      backgroundColor: palette.chartTooltip,
      borderColor: palette.chartTooltipBorder,
      textStyle: { color: palette.textPrimary, fontSize: 12 },
      confine: true,
    },
    legend: {
      type: "scroll", orient: "horizontal", bottom: 0,
      textStyle: { color: palette.textSecondary, fontSize: 11 },
    },
    series: [{
      type: "pie", radius: ["42%", "70%"], center: ["50%", "43%"],
      avoidLabelOverlap: true,
      label: { show: false },
      emphasis: { label: { show: true, formatter: "{b} {d}%" } },
      data: visibleItems.value.map((item, index) => ({
        name: item.name, value: item.value,
        itemStyle: { color: item.color || colors[index % colors.length] },
      })),
    }],
  }, true);
}

function resize(): void { chart?.resize(); }
watch(() => [props.items, theme.palette], () => void nextTick(render), { deep: true });
onMounted(() => {
  void nextTick(render);
  window.addEventListener("resize", resize);
  if (element.value && typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(element.value);
  }
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize);
  resizeObserver?.disconnect();
  chart?.dispose();
  chart = undefined;
});
</script>

<template>
  <div class="composition-chart" :style="{ height: `${height}px` }" data-test="composition-donut">
    <div v-if="title" class="chart-title">{{ title }}</div>
    <div ref="element" class="chart-root" role="img" :aria-label="accessibleSummary" />
    <div v-if="!hasData" class="chart-empty" data-test="composition-empty">暂无可计算的构成数据</div>
  </div>
</template>

<style scoped>
.composition-chart { position: relative; width: 100%; min-width: 0; }
.chart-title { position: absolute; top: 4px; left: 4px; z-index: 2; color: var(--ml-text-secondary); font-size: 12px; font-weight: 600; pointer-events: none; }
.chart-root { width: 100%; height: 100%; }
.chart-empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--ml-text-secondary); font-size: 12px; background: var(--ml-surface); }
</style>
