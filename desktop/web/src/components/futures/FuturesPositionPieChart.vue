<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useThemeStore } from "../../stores/theme";
import type { PositionPieSlice } from "../../domain/futuresMemberCharts";

const props = defineProps<{
  title: string;
  slices: PositionPieSlice[];
}>();
const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;
const positiveSlices = computed(() => props.slices.filter(item => Number.isFinite(item.value) && item.value > 0)
  .sort((left, right) => right.value - left.value || left.key.localeCompare(right.key)));

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char] || char));
}

function render(): void {
  if (!element.value) { chart?.dispose(); chart = undefined; return; }
  if (chart && chart.getDom() !== element.value) { chart.dispose(); chart = undefined; }
  chart ??= echarts.init(element.value);
  if (!positiveSlices.value.length) { chart.clear(); return; }
  const palette = theme.palette;
  chart.setOption({
    backgroundColor: "transparent",
    animationDuration: 220,
    aria: { enabled: true, decal: { show: false } },
    tooltip: {
      trigger: "item",
      formatter: (item: { name: string; value: number; percent: number }) =>
        `${escapeHtml(item.name)}：${item.value.toLocaleString("zh-CN")} 张（${item.percent.toFixed(1)}%）`,
      backgroundColor: palette.chartTooltip,
      borderColor: palette.chartTooltipBorder,
      textStyle: { color: palette.textPrimary },
      confine: true,
    },
    legend: {
      type: "scroll", orient: "horizontal", bottom: 0,
      textStyle: { color: palette.textSecondary, fontSize: 11 },
    },
    series: [{
      type: "pie", radius: ["32%", "70%"], center: ["50%", "43%"],
      avoidLabelOverlap: true, label: { show: false },
      emphasis: { label: { show: true, formatter: "{b}\n{d}%" } },
      data: positiveSlices.value.map(item => ({ name: item.name, value: item.value, id: item.key })),
    }],
  }, true);
}

function resize(): void { chart?.resize(); }
watch(() => [props.slices, theme.palette], () => void nextTick(render), { deep: true });
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
});
</script>

<template>
  <article class="position-chart-card">
    <h3>{{ title }}</h3>
    <div v-if="positiveSlices.length" ref="element" class="pie-chart" role="img" :aria-label="title" />
    <div v-else class="chart-empty">该日没有可计算的已公布持仓</div>
  </article>
</template>

<style scoped>
.position-chart-card { min-width: 0; padding: 12px; border: 1px solid var(--ml-divider); border-radius: 8px; background: var(--ml-surface-elevated); }
.position-chart-card h3 { margin: 0; font-size: 13px; }
.pie-chart { width: 100%; height: 310px; }
.chart-empty { display: grid; place-items: center; min-height: 270px; color: var(--ml-text-secondary); font-size: 12px; }
</style>
