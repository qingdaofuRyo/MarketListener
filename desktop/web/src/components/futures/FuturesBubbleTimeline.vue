<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { firstPlaybackIndex, futuresPlaybackWindows, type FuturesPlaybackWindow } from "../../domain/futuresPlaybackWindow";

const props = defineProps<{ dates: string[]; modelValue: number; resetKey: string; window?: FuturesPlaybackWindow }>();
const emit = defineEmits<{ (event: "update:modelValue", value: number): void; (event: "update:window", value: FuturesPlaybackWindow): void }>();
const playing = ref(false);
const speed = ref(0.5);
const windowMode = computed({ get: () => props.window ?? "60d", set: (value: FuturesPlaybackWindow) => emit("update:window", value) });
const hovering = ref(false);
const trackElement = ref<HTMLElement | null>(null);
let frame: number | undefined;
let currentPosition = props.modelValue;
let activePointerId: number | undefined;
const last = computed(() => Math.max(0, props.dates.length - 1));
const first = computed(() => firstPlaybackIndex(props.dates, windowMode.value));
const currentDay = computed(() => props.dates[Math.round(props.modelValue)] || "—");
const cursor = computed(() => last.value === first.value ? 100 : Math.max(0, Math.min(100, (props.modelValue - first.value) / (last.value - first.value) * 100)));
const ticks = computed(() => {
  const count = Math.min(5, Math.max(1, last.value - first.value + 1));
  return Array.from({ length: count }, (_, index) => {
    const position = count === 1 ? first.value : Math.round(first.value + index / (count - 1) * (last.value - first.value));
    return { position, label: props.dates[position] || "—" };
  });
});

function pause(snap = false): void {
  if (frame !== undefined) cancelAnimationFrame(frame);
  frame = undefined;
  playing.value = false;
  if (snap && props.dates.length) update(Math.round(currentPosition));
}

function update(value: number): void {
  currentPosition = Math.max(first.value, Math.min(last.value, value));
  emit("update:modelValue", currentPosition);
}

function toggle(): void {
  if (playing.value) { pause(true); return; }
  pause();
  if (last.value <= first.value) return;
  const start = currentPosition < first.value || currentPosition >= last.value ? first.value : currentPosition;
  update(start);
  playing.value = true;
  let previousFrame = performance.now();
  const step = (now: number) => {
    update(currentPosition + (now - previousFrame) * speed.value / 250);
    previousFrame = now;
    if (currentPosition >= last.value) { pause(); return; }
    frame = requestAnimationFrame(step);
  };
  frame = requestAnimationFrame(step);
}

function animateTo(target: number): void {
  pause();
  const start = currentPosition, started = performance.now();
  const step = (now: number) => {
    const progress = Math.min(1, (now - started) / 120);
    update(start + (target - start) * (1 - (1 - progress) ** 2));
    if (progress < 1) frame = requestAnimationFrame(step);
    else frame = undefined;
  };
  frame = requestAnimationFrame(step);
}

function browseAt(event: PointerEvent): void {
  const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
  if (!bounds.width) return;
  const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
  update(first.value + ratio * (last.value - first.value));
}

function beginBrowse(event: PointerEvent): void {
  if (!props.dates.length || activePointerId !== undefined || !event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
  pause();
  activePointerId = event.pointerId;
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  browseAt(event);
}

function moveBrowse(event: PointerEvent): void {
  if (event.pointerId !== activePointerId) return;
  browseAt(event);
}

function stopBrowse(): void {
  if (activePointerId === undefined) return;
  const pointerId = activePointerId;
  activePointerId = undefined;
  if (trackElement.value?.hasPointerCapture(pointerId)) trackElement.value.releasePointerCapture(pointerId);
}

function finishBrowse(event: PointerEvent): void {
  if (event.pointerId === activePointerId) { stopBrowse(); update(Math.round(currentPosition)); }
}

function lostCapture(event: PointerEvent): void {
  if (event.pointerId === activePointerId) { activePointerId = undefined; update(Math.round(currentPosition)); }
}

function keyboard(event: KeyboardEvent): void {
  let target: number | undefined;
  if (event.key === "ArrowLeft") target = Math.round(currentPosition) - 1;
  if (event.key === "ArrowRight") target = Math.round(currentPosition) + 1;
  if (event.key === "Home") target = first.value;
  if (event.key === "End") target = last.value;
  if (event.key === " ") { event.preventDefault(); toggle(); return; }
  if (target !== undefined) { event.preventDefault(); animateTo(target); }
}

function visibility(): void { if (document.hidden) pause(true); }
watch(() => props.modelValue, value => { currentPosition = value; });
watch(() => props.resetKey, () => { pause(true); stopBrowse(); });
watch(() => props.dates, () => { pause(); stopBrowse(); });
watch(windowMode, () => { pause(true); stopBrowse(); });
onMounted(() => document.addEventListener("visibilitychange", visibility));
onBeforeUnmount(() => { pause(); stopBrowse(); document.removeEventListener("visibilitychange", visibility); });
</script>

<template>
  <div class="bubble-timeline" :class="{ 'is-playing': playing }" data-test="futures-shared-playback" :data-position="modelValue">
    <div class="timeline-actions">
      <button class="step-button" type="button" :disabled="!dates.length || modelValue <= first" aria-label="上一个交易日" title="上一个交易日" @click="animateTo(Math.round(modelValue) - 1)"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12 5-5 5 5 5" /></svg></button>
      <button class="play-button" type="button" :disabled="dates.length < 2" :aria-pressed="playing" :aria-label="playing ? '暂停' : '播放'" @click="toggle"><span aria-hidden="true">{{ playing ? 'Ⅱ' : '▶' }}</span>{{ playing ? '暂停' : '播放' }}</button>
      <button class="step-button" type="button" :disabled="!dates.length || modelValue >= last" aria-label="下一个交易日" title="下一个交易日" @click="animateTo(Math.round(modelValue) + 1)"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m8 5 5 5-5 5" /></svg></button>
      <select v-model.number="speed" aria-label="期货图表播放倍速"><option :value="0.5">0.5×</option><option :value="1">1×</option><option :value="2">2×</option><option :value="4">4×</option></select>
      <select v-model="windowMode" aria-label="期货图表日期范围"><option v-for="item in futuresPlaybackWindows" :key="item.value" :value="item.value">{{ item.label }}</option></select>
      <time>{{ currentDay }}</time>
    </div>
    <div ref="trackElement" class="date-track" :class="{ hovering }" data-test="futures-shared-date-track" role="slider" tabindex="0" aria-label="期货图表日期时间槽"
      :aria-valuemin="first" :aria-valuemax="last" :aria-valuenow="Math.round(modelValue)" :aria-valuetext="currentDay"
      @pointerenter="hovering = true" @pointerleave="hovering = false"
      @pointermove="moveBrowse" @pointerdown="beginBrowse" @pointerup="finishBrowse" @pointercancel="finishBrowse" @lostpointercapture="lostCapture" @keydown="keyboard">
      <div class="track-line" /><div class="track-fill" :style="{ width: `${cursor}%` }" />
      <div class="track-caption" :style="{ left: `${cursor}%`, transform: cursor < 10 ? 'none' : cursor > 90 ? 'translateX(-100%)' : 'translateX(-50%)' }">{{ currentDay }}</div>
      <div class="track-cursor" :style="{ left: `${cursor}%` }" />
      <div class="date-ticks"><span v-for="tick in ticks" :key="tick.position">{{ tick.label }}</span></div>
    </div>
  </div>
</template>

<style scoped>
.bubble-timeline { display: grid; grid-template-columns: auto minmax(180px,1fr); gap: 20px; align-items: center; padding: 7px 12px; border: 1px solid var(--ml-divider); border-radius: 10px; background: var(--ml-surface-elevated); }
.timeline-actions { display: flex; align-items: center; gap: 6px; min-width: 0; white-space: nowrap; }
button,select { min-height: 32px; border: 1px solid var(--ml-divider); border-radius: 7px; padding: 4px 8px; color: var(--ml-text-primary); background: var(--ml-surface-elevated); font-size: 12px; transition: border-color 150ms,background-color 150ms; }
button { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; cursor: pointer; }
button:hover:not(:disabled),select:hover { border-color: var(--ml-accent); background: var(--ml-surface-selected); }
button:focus-visible,select:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; }
.play-button { padding-inline: 9px 12px; color: var(--ml-accent); border-color: color-mix(in srgb,var(--ml-accent) 22%,var(--ml-divider)); background: var(--ml-accent-soft); }
.play-button:hover:not(:disabled) { background: color-mix(in srgb,var(--ml-accent) 16%,var(--ml-surface)); }
.step-button { justify-content: center; width: 29px; min-height: 30px; padding: 4px; border-color: transparent; background: transparent; }
.step-button svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
button span { display: grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; background: var(--ml-accent-soft); color: var(--ml-accent); font-size: 11px; }
button:disabled { opacity: .5; cursor: default; }
select { cursor: pointer; }
time { padding: 6px 8px; margin-left: 3px; border-left: 1px solid var(--ml-divider); color: var(--ml-text-primary); font: 600 12px ui-monospace,Consolas,monospace; font-variant-numeric: tabular-nums; }
.date-track { position: relative; min-width: 0; height: 44px; margin: 0 2px; cursor: ew-resize; touch-action: none; user-select: none; }
.track-line,.track-fill { position: absolute; top: 14px; left: 0; height: 10px; border-radius: 99px; pointer-events: none; }
.track-line { right: 0; background: color-mix(in srgb,var(--ml-chart-secondary) 20%,var(--ml-surface-selected)); box-shadow: inset 0 1px 2px rgba(14,10,28,.1); }
.track-fill { overflow: hidden; background: linear-gradient(100deg, #b24e75 0%, #be68ad 38%, #b984ec 70%, #8067dd 100%); background-size: 190% 100%; box-shadow: inset 0 1px rgba(255,255,255,.25); }
.track-fill::after { content: ''; position: absolute; inset: 0; background-image: radial-gradient(circle,rgba(255,255,255,.9) 0 1px,transparent 1.4px),radial-gradient(circle,rgba(255,255,255,.55) 0 .7px,transparent 1.2px); background-size: 45px 10px,67px 10px; background-position: 12px 1px,36px 5px; opacity: .5; }
.is-playing .track-fill { animation: track-flow 4s ease-in-out infinite alternate; }
.is-playing .track-fill::after { animation: track-sparkle 3.2s linear infinite; }
.track-cursor { position: absolute; top: 9px; width: 20px; height: 20px; transform: translateX(-50%); border: 2px solid #fff; border-radius: 50%; background: #fff; box-shadow: 0 0 0 1px rgba(115,78,187,.45),0 2px 7px rgba(25,40,75,.2); pointer-events: none; transition: box-shadow 150ms,transform 150ms; }
.hovering .track-cursor,.date-track:focus-visible .track-cursor { box-shadow: 0 0 0 4px rgba(177,105,223,.18),0 2px 7px rgba(25,40,75,.2); }
.track-caption { position: absolute; top: -24px; padding: 3px 8px; border: 1px solid var(--ml-divider); border-radius: 7px; color: var(--ml-text-primary); background: var(--ml-surface-elevated); box-shadow: 0 3px 10px rgba(25,40,75,.1); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; pointer-events: none; opacity: 0; transition: opacity 130ms; }
.hovering .track-caption,.date-track:focus-visible .track-caption { opacity: 1; }
.date-track:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 3px; border-radius: 9px; }
.date-ticks { position: absolute; bottom: 0; left: 0; right: 0; display: flex; justify-content: space-between; color: var(--ml-text-secondary); font-size: 10px; font-variant-numeric: tabular-nums; pointer-events: none; }
@media (max-width: 1000px) { .bubble-timeline { grid-template-columns: minmax(0,1fr); gap: 5px; padding: 2px 0; }.date-track { height: 32px; }.track-line,.track-fill { top: 5px; }.track-cursor { top: 0; }.date-ticks { font-size: 9px; }.date-ticks span:nth-child(2),.date-ticks span:nth-child(4) { display: none; }.timeline-actions { justify-content: center; } }
@media (max-width: 600px) { .timeline-actions { flex-wrap: wrap; justify-content: flex-start; gap: 4px; }.timeline-actions time { margin-left: auto; padding-inline: 5px; }button,select { font-size: 11px; padding-inline: 6px; }.step-button { width: 22px; }.play-button { padding-inline: 6px 9px; } }
@keyframes track-flow { to { background-position: 100% 0; } }
@keyframes track-sparkle { to { background-position: 57px 1px,103px 5px; } }
@media (prefers-reduced-motion: reduce) { button,select,.track-cursor { transition: none; }.is-playing .track-fill,.is-playing .track-fill::after { animation: none; } }
</style>
