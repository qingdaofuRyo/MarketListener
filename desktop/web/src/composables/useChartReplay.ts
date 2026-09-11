import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { advanceReplay, type ReplayState } from "../domain/chartReplay";

export function useChartReplay(total: Ref<number>) {
  const state = ref<ReplayState>("closed");
  const count = ref(0);
  const speed = ref(1);
  const following = ref(true);
  let timer: ReturnType<typeof setInterval> | undefined;
  const active = computed(() => state.value !== "closed");
  const selecting = computed(() => state.value === "selecting");
  const playing = computed(() => state.value === "playing");
  function pause() { if (playing.value) state.value = "paused"; }
  function close() { state.value = "closed"; }
  function select() { state.value = "selecting"; following.value = true; }
  function pick(index: number) {
    if (!Number.isInteger(index) || index < 0 || index >= total.value) return;
    count.value = index + 1;
    state.value = count.value >= total.value ? "ended" : "paused";
  }
  function step(amount = 1) {
    if (!active.value || selecting.value) return;
    count.value = advanceReplay(count.value, total.value, amount);
    if (count.value >= total.value) state.value = "ended";
  }
  function togglePlaying() {
    if (playing.value) pause();
    else if (state.value === "paused" && count.value < total.value) state.value = "playing";
  }
  watch([state, speed], () => {
    if (timer) clearInterval(timer);
    timer = undefined;
    if (playing.value) timer = setInterval(() => step(), 1000 / speed.value);
  }, { flush: "sync" });
  function visibility() { if (document.hidden) pause(); }
  document.addEventListener("visibilitychange", visibility);
  onBeforeUnmount(() => { if (timer) clearInterval(timer); document.removeEventListener("visibilitychange", visibility); });
  return { state, count, speed, following, active, selecting, playing, pause, close, select, pick, step, togglePlaying };
}
