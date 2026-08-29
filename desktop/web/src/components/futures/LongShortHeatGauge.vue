<script setup lang="ts">
import * as echarts from "echarts";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { signedHeat } from "../../domain/futures";
import type { LongShortHeatStateBand } from "../../domain/futures";
import { useThemeStore } from "../../stores/theme";

const props = withDefaults(defineProps<{
  title: string;
  value: number | null;
  status: string;
  bands: LongShortHeatStateBand[];
  min?: number;
  max?: number;
}>(), { min: -100, max: 100 });

const theme = useThemeStore();
const element = ref<HTMLElement>();
let chart: echarts.ECharts | undefined;
let resizeObserver: ResizeObserver | undefined;

const displayValue = computed(() => signedHeat(props.value));
const numericValue = computed(() =>
  typeof props.value === "number" && Number.isFinite(props.value)
    ? Math.max(props.min, Math.min(props.max, props.value))
    : null,
);
const directionClass = computed(() => {
  if (numericValue.value === null || numericValue.value === 0) return "neutral";
  return numericValue.value > 0 ? "long" : "short";
});

function render(): void {
  if (!element.value) return;
  chart ??= echarts.init(element.value);
  if (numericValue.value === null) {
    chart.clear();
    return;
  }
  const palette = theme.palette;
  const bandColors = [
    palette.heatExtremeShort,
    palette.heatShort,
    palette.heatMildShort,
    palette.heatNeutral,
    palette.heatMildLong,
    palette.heatLong,
    palette.heatExtremeLong,
  ];
  const axisColors = props.bands.map((band, index) => [
    (band.max - props.min) / (props.max - props.min),
    bandColors[Math.min(index, bandColors.length - 1)],
  ] as [number, string]);
  chart.setOption({
    backgroundColor: "transparent",
    animationDuration: 300,
    animationDurationUpdate: 220,
    animationEasingUpdate: "cubicOut",
    aria: { enabled: true, decal: { show: false } },
    series: [{
      name: props.title,
      type: "gauge",
      min: props.min,
      max: props.max,
      startAngle: 180,
      endAngle: 0,
      center: ["50%", "72%"],
      radius: "112%",
      splitNumber: 4,
      axisLine: {
        lineStyle: {
          width: 15,
          color: axisColors,
        },
      },
      pointer: {
        show: true,
        length: "57%",
        width: 5,
        itemStyle: { color: palette.textPrimary },
      },
      anchor: {
        show: true,
        size: 8,
        itemStyle: { color: palette.textPrimary, borderColor: palette.surface, borderWidth: 2 },
      },
      axisTick: { distance: -20, length: 4, lineStyle: { color: palette.surface, width: 1 } },
      splitLine: { distance: -21, length: 9, lineStyle: { color: palette.surface, width: 2 } },
      axisLabel: {
        distance: -36,
        color: palette.textSecondary,
        fontSize: 10,
        formatter: (value: number) => `${value > 0 ? "+" : ""}${value}`,
      },
      title: { show: false },
      detail: { show: false },
      data: [{ value: numericValue.value, name: props.title }],
    }],
  }, true);
}

function resize(): void {
  chart?.resize();
}

watch(
  () => [props.value, props.min, props.max, props.bands, theme.palette],
  () => void nextTick(render),
  { deep: true },
);

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
  <article
    class="gauge-card"
    :class="directionClass"
    role="img"
    :aria-label="`${title}：${displayValue}，${status}`"
    :data-current-value="numericValue ?? ''"
    data-test="heat-gauge"
  >
    <h3>{{ title }}</h3>
    <div ref="element" class="gauge-chart" />
    <div class="gauge-reading">
      <strong>{{ displayValue }}</strong>
      <span>{{ status }}</span>
    </div>
  </article>
</template>

<style scoped>
.gauge-card {
  position: relative;
  min-width: 0;
  height: 235px;
  padding: 12px 12px 8px;
  overflow: hidden;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
  background: var(--ml-surface-elevated);
}
.gauge-card h3 { position: relative; z-index: 2; margin: 0; text-align: center; font-size: 13px; }
.gauge-chart { position: absolute; inset: 30px 5px 0; }
.gauge-reading {
  position: absolute;
  z-index: 2;
  left: 50%;
  bottom: 23px;
  display: flex;
  align-items: center;
  flex-direction: column;
  transform: translateX(-50%);
  pointer-events: none;
}
.gauge-reading strong { font: 750 25px/1.1 ui-monospace, Consolas, monospace; color: var(--ml-flat); }
.gauge-reading span { margin-top: 5px; color: var(--ml-text-secondary); font-size: 12px; white-space: nowrap; }
.gauge-card.long .gauge-reading strong { color: var(--ml-price-up); }
.gauge-card.short .gauge-reading strong { color: var(--ml-price-down); }
@media (max-width: 700px) { .gauge-card { height: 220px; } }
</style>
