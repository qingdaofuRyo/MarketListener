<script setup lang="ts">
import { computed, ref } from "vue";
import { DRAWING_COLOR_PRESETS } from "./drawingPalette";

const props = withDefaults(defineProps<{ modelValue?: string; checkerboard?: boolean; title?: string; presets?: readonly string[] }>(), {
  modelValue: "#2962ff",
  checkerboard: false,
  title: "",
  presets: () => [...DRAWING_COLOR_PRESETS],
});
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const open = ref(false);

interface Rgba { r: number; g: number; b: number; a: number }

function parseColor(value: string): Rgba {
  const text = value.trim();
  const rgb = text.match(/^rgba?\(([^)]+)\)$/i);
  if (rgb) {
    const parts = rgb[1].split(/[,/]/).map((part) => Number.parseFloat(part.trim()));
    return { r: parts[0] ?? 41, g: parts[1] ?? 98, b: parts[2] ?? 255, a: parts.length > 3 ? parts[3] : 1 };
  }
  let hex = text.replace("#", "");
  if (hex.length === 3) hex = hex.split("").map((character) => character + character).join("");
  if (/^[0-9a-f]{6}$/i.test(hex)) {
    return {
      r: Number.parseInt(hex.slice(0, 2), 16),
      g: Number.parseInt(hex.slice(2, 4), 16),
      b: Number.parseInt(hex.slice(4, 6), 16),
      a: 1,
    };
  }
  return { r: 41, g: 98, b: 255, a: 1 };
}

function toHex(channel: number): string {
  return Math.max(0, Math.min(255, Math.round(channel))).toString(16).padStart(2, "0");
}

const hexValue = computed(() => {
  const color = parseColor(props.modelValue);
  return `#${toHex(color.r)}${toHex(color.g)}${toHex(color.b)}`;
});
const alphaValue = computed(() => parseColor(props.modelValue).a);
const alphaGradient = computed(() => {
  const color = parseColor(props.modelValue);
  return `linear-gradient(to right, rgba(${color.r},${color.g},${color.b},0), ${hexValue.value})`;
});

function emitColor(hex: string, alpha: number): void {
  const match = hex.match(/^#?([0-9a-f]{6})$/i);
  if (!match) return;
  const r = Number.parseInt(match[1].slice(0, 2), 16);
  const g = Number.parseInt(match[1].slice(2, 4), 16);
  const b = Number.parseInt(match[1].slice(4, 6), 16);
  const a = Math.max(0, Math.min(1, alpha));
  emit("update:modelValue", `rgba(${r},${g},${b},${a.toFixed(3)})`);
}

function checkerboardColor(index: number): string {
  return (index + Math.floor((index - 1) / 4)) % 2 === 0 ? props.modelValue : "#ffffff";
}
</script>

<template>
  <el-popover
    v-model:visible="open"
    trigger="click"
    :width="306"
    placement="bottom"
    :show-arrow="false"
    :popper-style="{ zIndex: 3200 }"
    popper-class="drawing-color-popover"
  >
    <template #reference>
      <button type="button" class="color-trigger" :class="{ checkerboard }" :aria-label="title" :title="title">
        <span v-if="checkerboard" class="checkerboard"><i v-for="index in 12" :key="index" :style="{ backgroundColor: checkerboardColor(index) }" /></span>
        <span v-else class="plain-swatch" :style="{ backgroundColor: modelValue }" />
      </button>
    </template>
    <div class="color-picker-body" @keydown.esc.stop="open = false">
      <div v-if="presets.length" class="preset-grid" role="group" :aria-label="`${title}预置颜色`">
        <button
          v-for="color in presets"
          :key="color"
          type="button"
          :class="{ active: hexValue.toLowerCase() === color.toLowerCase() }"
          :style="{ backgroundColor: color }"
          :aria-pressed="hexValue.toLowerCase() === color.toLowerCase()"
          :aria-label="`${title}颜色 ${color}`"
          :title="color"
          @click="emitColor(color, alphaValue)"
        />
      </div>
      <label class="custom-color" aria-label="自定义颜色"><span class="custom-swatch" :style="{ backgroundColor: modelValue }" /> <span aria-hidden="true">+</span><input class="hue-input" type="color" :value="hexValue" aria-label="自定义颜色选择器" @input="emitColor(($event.target as HTMLInputElement).value, alphaValue)" /></label>
      <div class="alpha-row">
        <span class="alpha-track" :style="{ '--alpha-gradient': alphaGradient }">
          <input type="range" min="0" max="1" step="0.01" :value="alphaValue" :style="{ '--alpha-thumb-color': modelValue }" aria-label="不透明度" @input="emitColor(hexValue, Number(($event.target as HTMLInputElement).value))" />
        </span>
        <span class="alpha-value">{{ Math.round(alphaValue * 100) }}%</span>
      </div>
    </div>
  </el-popover>
</template>

<style scoped>
.color-trigger {
  display: grid;
  place-items: center;
  width: 28px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--ml-divider);
  border-radius: 4px;
  background: var(--ml-background);
  cursor: pointer;
}
.plain-swatch {
  display: block;
  width: 20px;
  height: 16px;
  border-radius: 2px;
}
.checkerboard {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  grid-template-rows: repeat(3, 1fr);
  width: 20px;
  height: 16px;
  padding: 1px;
  gap: 1px;
  overflow: hidden;
  border-radius: 2px;
}
.checkerboard i {
  display: block;
  min-width: 0;
  min-height: 0;
}
.color-picker-body {
  display: grid;
  gap: 8px;
}
.preset-grid {
  display: grid;
  grid-template-columns: repeat(10, 24px);
  gap: 3px;
  justify-content: center;
}
.preset-grid button {
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--ml-text-primary) 24%, transparent);
  border-radius: 3px;
  cursor: pointer;
}
.preset-grid button.active {
  box-shadow: 0 0 0 2px var(--ml-accent), inset 0 0 0 1px #fff;
}
.preset-grid button:focus-visible,
.color-trigger:focus-visible,
.hue-input:focus-visible,
.alpha-track input:focus-visible {
  outline: 2px solid var(--ml-accent);
  outline-offset: 2px;
}
.custom-color { display:flex; align-items:center; gap:6px; color:var(--ml-text-secondary); font-size:12px; }
.custom-swatch { width:22px; height:22px; border:1px solid var(--ml-divider); border-radius:3px; }
.hue-input {
  width: 100%; height: 28px; padding: 2px; border: 1px solid var(--ml-divider); border-radius: 4px; background: transparent; cursor: pointer;
}
.alpha-row {
  display: grid;
  grid-template-columns: 1fr 34px;
  align-items: center;
  gap: 8px;
}
.alpha-track {
  position: relative;
  display: block;
  height: 12px;
  border: 1px solid var(--ml-divider);
  border-radius: 6px;
  background-color: #ffffff;
  background-image: var(--alpha-gradient), repeating-conic-gradient(#ffffff 0 25%, #d8d8d8 0 50%);
  background-size: 100% 100%, 8px 8px;
}
.alpha-track input {
  position: absolute;
  inset: 0;
  width: 100%;
  margin: 0;
  appearance: none;
  border: 0;
  background: transparent;
  cursor: pointer;
}
.alpha-track input::-webkit-slider-runnable-track { height: 12px; background: transparent; }
.alpha-track input::-webkit-slider-thumb {
  width: 14px;
  height: 14px;
  margin-top: -1px;
  appearance: none;
  border: 2px solid rgba(255,255,255,.9);
  border-radius: 50%;
  background: var(--alpha-thumb-color);
  box-shadow: 0 0 0 1px rgba(0,0,0,.55), 0 1px 3px rgba(0,0,0,.35);
}
.alpha-track input::-moz-range-track { height: 12px; border: 0; background: transparent; }
.alpha-track input::-moz-range-thumb {
  width: 11px;
  height: 11px;
  border: 2px solid rgba(255,255,255,.9);
  border-radius: 50%;
  background: var(--alpha-thumb-color);
  box-shadow: 0 0 0 1px rgba(0,0,0,.55), 0 1px 3px rgba(0,0,0,.35);
}
.alpha-value {
  color: var(--ml-text-secondary);
  font: 10px/1 ui-monospace, Consolas, monospace;
  text-align: right;
}
</style>
