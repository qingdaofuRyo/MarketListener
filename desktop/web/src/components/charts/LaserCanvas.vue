<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";

const canvas = ref<HTMLCanvasElement>();
let pointer: number | undefined;
let frame = 0;
let points: Array<{ x: number; y: number; time: number; start: boolean }> = [];
const lifetime = 1200;
function paint(now: number): void {
  frame = 0;
  const el = canvas.value;
  if (!el) return;
  const box = el.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  if (el.width !== Math.round(box.width * dpr) || el.height !== Math.round(box.height * dpr)) {
    el.width = Math.round(box.width * dpr); el.height = Math.round(box.height * dpr);
  }
  const ctx = el.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, box.width, box.height);
  points = points.filter((point) => now - point.time < lifetime);
  el.dataset.pointCount = String(points.length);
  ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.lineWidth = 3;
  ctx.shadowColor = "#ff345b"; ctx.shadowBlur = 10;
  for (let index = 1; index < points.length; index++) {
    const a = points[index - 1], b = points[index];
    if (b.start) continue;
    ctx.strokeStyle = `rgba(255,52,91,${Math.max(0, 1 - (now - b.time) / lifetime)})`;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  if (points.length) frame = requestAnimationFrame(paint);
}
function append(event: PointerEvent, start = false): void {
  const box = canvas.value!.getBoundingClientRect();
  points.push({ x: event.clientX - box.left, y: event.clientY - box.top, time: performance.now(), start });
  if (points.length > 4096) points = points.slice(-4096);
  if (!frame) frame = requestAnimationFrame(paint);
}
function down(event: PointerEvent): void {
  if (event.button !== 0) return;
  pointer = event.pointerId; canvas.value!.setPointerCapture(pointer); append(event, true);
}
function move(event: PointerEvent): void {
  if (pointer !== event.pointerId) return;
  const samples = event.getCoalescedEvents?.();
  for (const sample of samples?.length ? samples : [event]) append(sample);
}
function up(event: PointerEvent): void {
  if (event.pointerId !== pointer) return;
  append(event); pointer = undefined;
  if (canvas.value?.hasPointerCapture(event.pointerId)) canvas.value.releasePointerCapture(event.pointerId);
}
onBeforeUnmount(() => { cancelAnimationFrame(frame); points = []; });
</script>
<template>
  <canvas ref="canvas" class="laser-canvas" aria-label="激光笔画布" data-point-count="0"
    @pointerdown.stop.prevent="down" @pointermove.stop.prevent="move"
    @pointerup.stop.prevent="up" @pointercancel.stop.prevent="up" />
</template>
<style scoped>
.laser-canvas { position: absolute; inset: 0; width: 100%; height: 100%; z-index: 12; cursor: crosshair; touch-action: none; }
</style>
