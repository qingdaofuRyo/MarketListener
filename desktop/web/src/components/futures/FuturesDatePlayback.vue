<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { firstPlaybackIndex, futuresPlaybackWindows, type FuturesPlaybackWindow } from "../../domain/futuresPlaybackWindow";
const props = defineProps<{ dates: string[]; modelValue: number; resetKey: string; window?: FuturesPlaybackWindow }>();
const emit = defineEmits<{ (event: "update:modelValue", value: number): void; (event: "update:window", value: FuturesPlaybackWindow): void }>();
const playing = ref(false);
const speed = ref(2);
const windowMode = computed({ get: () => props.window ?? "60d", set: (value: FuturesPlaybackWindow) => emit("update:window", value) });
const first = computed(() => firstPlaybackIndex(props.dates, windowMode.value));
const last = computed(() => Math.max(0, props.dates.length - 1));
const progress = computed(() => first.value >= last.value ? 100
  : Math.max(0, Math.min(100, (props.modelValue - first.value) / (last.value - first.value) * 100)));
const progressDuration = computed(() => `${Math.max(40, Math.round(225 / speed.value))}ms`);
let timer: ReturnType<typeof setInterval> | undefined;
function pause(): void { if (timer) clearInterval(timer); timer = undefined; playing.value = false; }
function intervalMs(): number { return 250 / speed.value; }
function advance(): void {
  const next = props.modelValue + 1;
  if (next > last.value) { pause(); return; }
  emit("update:modelValue", next);
  if (next === last.value) pause();
}
function schedule(): void {
  if (timer) clearInterval(timer);
  timer = setInterval(advance, intervalMs());
}
function toggle(): void {
  if (playing.value) { pause(); return; }
  if (last.value <= first.value) return;
  if (props.modelValue >= last.value || props.modelValue < first.value) emit("update:modelValue", first.value);
  playing.value = true;
  schedule();
}
function drag(event: Event): void { pause(); emit("update:modelValue", Number((event.target as HTMLInputElement).value)); }
function visibility(): void { if (document.hidden) pause(); }
watch(speed, () => { if (playing.value) schedule(); });
watch(windowMode, () => { pause(); emit("update:modelValue", Math.max(first.value, Math.min(last.value, props.modelValue))); });
watch(() => props.resetKey, pause);
watch(() => props.dates, pause);
onMounted(() => document.addEventListener("visibilitychange", visibility));
onBeforeUnmount(() => { pause(); document.removeEventListener("visibilitychange", visibility); });
</script>
<template>
  <div class="playback" :class="{ 'is-playing': playing }" data-test="ranking-playback">
    <button class="transport" :disabled="dates.length < 2" :aria-pressed="playing" @click="toggle"><span class="transport-icon" aria-hidden="true">{{ playing ? 'Ⅱ' : '▶' }}</span>{{ playing ? '暂停' : '播放' }}</button>
    <select v-model.number="speed" aria-label="席位播放倍速"><option :value="0.5">0.5×</option><option :value="1">1×</option><option :value="2">2×</option><option :value="4">4×</option></select>
    <select v-model="windowMode" aria-label="席位日期范围"><option v-for="item in futuresPlaybackWindows" :key="item.value" :value="item.value">{{ item.label }}</option></select>
    <div class="timeline" :style="{ '--playback-progress': `${progress}%`, '--playback-duration': progressDuration }">
      <div class="timeline-track" aria-hidden="true"><span class="timeline-progress" /><span class="timeline-head" /></div>
      <input type="range" aria-label="排名日期" :min="first" :max="last" :value="modelValue" :disabled="!dates.length" @pointerdown="pause" @input="drag" />
      <div class="timeline-labels" aria-hidden="true"><span>{{ dates[first] || '—' }}</span><span>{{ dates[last] || '—' }}</span></div>
    </div>
    <time class="playback-date"><span v-if="playing" class="playback-pulse" aria-hidden="true" />{{ dates[modelValue] || '—' }}</time>
  </div>
</template>
<style scoped>
.playback { display: flex; gap: 10px; align-items: center; margin: 16px 0; min-width: 0; padding: 13px 15px; border: 1px solid var(--ml-divider); border-radius: 14px; background: linear-gradient(145deg,var(--ml-surface-elevated),color-mix(in srgb,var(--ml-surface-selected) 42%,var(--ml-surface-elevated))); box-shadow: 0 7px 22px rgba(20,35,70,.045); }
.transport,select { min-height: 35px; border: 1px solid var(--ml-divider); border-radius: 9px; color: var(--ml-text-primary); background: var(--ml-surface-elevated); transition: border-color 150ms,box-shadow 150ms,background-color 150ms; }
.transport { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-width: 77px; padding: 5px 11px; font-weight: 600; cursor: pointer; }
.transport:hover:not(:disabled),select:hover,.is-playing .transport { border-color: var(--ml-accent); box-shadow: 0 0 0 3px var(--ml-accent-soft); }
.transport:disabled { opacity: .5; cursor: default; }
.transport-icon { display: grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; background: var(--ml-accent-soft); color: var(--ml-accent); font-size: 11px; line-height: 1; }
select { padding: 5px 9px; font-variant-numeric: tabular-nums; cursor: pointer; }
.timeline { position: relative; flex: 1; min-width: 120px; height: 45px; }
.timeline-track { position: absolute; inset: 12px 9px auto; height: 9px; border-radius: 99px; background: var(--ml-divider); box-shadow: inset 0 2px 4px rgba(20,35,70,.11); pointer-events: none; }
.timeline-progress { display: block; width: var(--playback-progress); height: 100%; border-radius: inherit; background: linear-gradient(90deg,color-mix(in srgb,var(--ml-accent) 55%,var(--ml-surface-elevated)),var(--ml-accent)); box-shadow: 0 2px 8px var(--ml-accent-soft); }
.timeline-head { position: absolute; top: -7px; left: var(--playback-progress); width: 23px; height: 23px; transform: translateX(-50%); border: 4px solid var(--ml-surface-elevated); border-radius: 50%; background: var(--ml-accent); box-shadow: 0 0 0 1px var(--ml-accent),0 4px 10px rgba(20,35,70,.18); }
.is-playing .timeline-progress { transition: width var(--playback-duration) linear; }
.is-playing .timeline-head { transition: left var(--playback-duration) linear; }
.timeline input { position: absolute; inset: 0 0 auto; width: 100%; height: 32px; margin: 0; appearance: none; background: transparent; cursor: ew-resize; }
.timeline input::-webkit-slider-runnable-track { height: 9px; background: transparent; }
.timeline input::-moz-range-track { height: 9px; background: transparent; }
.timeline input::-webkit-slider-thumb { width: 22px; height: 22px; margin-top: -7px; appearance: none; border: 0; background: transparent; }
.timeline input::-moz-range-thumb { width: 22px; height: 22px; border: 0; background: transparent; }
.timeline input:focus-visible { outline: 2px solid var(--ml-accent); outline-offset: 2px; border-radius: 7px; }
.timeline input:disabled { cursor: default; opacity: .5; }
.timeline-labels { position: absolute; bottom: 0; left: 7px; right: 7px; display: flex; justify-content: space-between; color: var(--ml-text-secondary); font: 11px ui-monospace,Consolas,monospace; pointer-events: none; }
.playback-date { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-width: 108px; padding: 7px 9px; border: 1px solid var(--ml-divider); border-radius: 9px; color: var(--ml-text-primary); background: var(--ml-surface-selected); font-variant-numeric: tabular-nums; font-size: 12px; white-space: nowrap; }
.is-playing .playback-date { color: var(--ml-text-primary); border-color: var(--ml-accent); }
.playback-pulse { width: 6px; height: 6px; border-radius: 50%; background: var(--ml-accent); box-shadow: 0 0 0 0 var(--ml-accent-soft); animation: playback-pulse 1.5s ease-out infinite; }
@keyframes playback-pulse { 50% { box-shadow: 0 0 0 5px var(--ml-accent-soft); } }
@media (max-width: 850px) { .playback { flex-wrap: wrap; }.timeline { order: 5; flex-basis: 100%; }.playback-date { margin-left: auto; } }
@media (prefers-reduced-motion: reduce) { .is-playing .timeline-progress, .is-playing .timeline-head { transition: none; }.playback-pulse { animation: none; } }
</style>
