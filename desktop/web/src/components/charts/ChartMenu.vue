<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import ChartIcon from "./ChartIcon.vue";
export interface ChartMenuItem { id: string; label: string; icon?: string; path?: string; disabled?: boolean }
const props = defineProps<{ anchor: HTMLElement; label: string; selected: string; groups: Array<{ label: string; items: ChartMenuItem[] }>; side?: "left" | "bottom" }>();
const emit = defineEmits<{ select: [id: string]; close: [] }>();
const root = ref<HTMLElement>();
const restoreFocus = ref(false);
const position = ref({ left: "0px", top: "0px" });
function place() {
  if (!root.value) return;
  const anchor = props.anchor.getBoundingClientRect();
  const rect = root.value.getBoundingClientRect();
  const left = props.side === "left" ? anchor.left - rect.width - 5 : anchor.left;
  position.value = { left: `${Math.max(4, Math.min(left, innerWidth - rect.width - 4))}px`, top: `${Math.max(4, Math.min(props.side === "left" ? anchor.top : anchor.bottom + 4, innerHeight - rect.height - 4))}px` };
}
function outside(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node) && !props.anchor.contains(event.target as Node)) emit("close");
}
function keyboard(event: KeyboardEvent) {
  if (event.key === "Escape" || event.key === "Tab") { restoreFocus.value = event.key === "Escape"; if (restoreFocus.value) event.preventDefault(); event.stopPropagation(); emit("close"); return; }
  const buttons = Array.from(root.value?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") || []);
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
  if (!buttons.length || (!step && !["Home", "End"].includes(event.key))) return;
  event.preventDefault(); event.stopPropagation();
  buttons[event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + step + buttons.length) % buttons.length]?.focus();
}
onMounted(() => {
  void nextTick(() => { place(); (root.value?.querySelector("button.active:not(:disabled)") as HTMLElement || root.value?.querySelector("button:not(:disabled)") as HTMLElement)?.focus(); });
  window.addEventListener("resize", place); document.addEventListener("pointerdown", outside);
});
onBeforeUnmount(() => { window.removeEventListener("resize", place); document.removeEventListener("pointerdown", outside); if (restoreFocus.value && props.anchor.isConnected) props.anchor.focus(); });
</script>
<template><Teleport to="body"><div ref="root" class="chart-floating-menu" role="menu" :aria-label="label" :style="position" @keydown="keyboard">
  <section v-for="group in groups" :key="group.label" role="group" :aria-label="group.label"><h4>{{ group.label }}</h4>
    <button v-for="item in group.items" :key="item.id" role="menuitem" :aria-label="item.label" :aria-current="selected === item.id ? 'true' : undefined" :class="{active:selected===item.id}" :disabled="item.disabled" @click="restoreFocus=true;emit('select',item.id)">
      <svg v-if="item.path" viewBox="0 0 24 24" aria-hidden="true"><path :d="item.path"/></svg><ChartIcon v-else :name="item.icon || item.id"/><span>{{ item.label }}</span>
    </button>
  </section>
</div></Teleport></template>
<style scoped>
.chart-floating-menu{position:fixed;z-index:3000;box-sizing:border-box;width:224px;max-width:calc(100vw - 8px);max-height:calc(100dvh - 8px);overflow:auto;padding:6px;background:var(--ml-surface);color:var(--ml-text-primary);border:1px solid var(--ml-divider);border-radius:7px;box-shadow:0 6px 24px #0003}
section+section{border-top:1px solid var(--ml-divider);margin-top:5px;padding-top:5px}h4{font-size:11px;font-weight:400;color:var(--ml-text-secondary);margin:5px 8px}
button{display:grid;grid-template-columns:24px minmax(0,1fr);gap:9px;align-items:center;width:100%;padding:9px 8px;border:0;border-radius:5px;background:transparent;color:inherit;text-align:left;cursor:pointer}
button:hover,button.active{background:var(--ml-surface-selected)}button:focus-visible{outline:2px solid var(--ml-accent);outline-offset:-2px}button:disabled{opacity:.4;cursor:default}svg{width:21px;height:21px;stroke:currentColor;stroke-width:1.5;fill:none}
</style>
