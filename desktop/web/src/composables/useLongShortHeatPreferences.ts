import { computed, ref, type Ref, watch } from "vue";
import type { LongShortHeatWeightRange } from "../domain/futures";

export const LONG_SHORT_HEAT_WEIGHT_KEY = "marketlistener.futures.longShortHeat.fundWeight";

function normalizeWeight(value: unknown, fallback: number, range: LongShortHeatWeightRange): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  const decimal = parsed > 1 ? parsed / 100 : parsed;
  const clamped = Math.max(range.min, Math.min(range.max, decimal));
  const stepped = range.min + Math.round((clamped - range.min) / range.step) * range.step;
  return Math.round(Math.max(range.min, Math.min(range.max, stepped)) * 1e12) / 1e12;
}

function storedWeight(range: LongShortHeatWeightRange): number | null {
  try {
    const raw = localStorage.getItem(LONG_SHORT_HEAT_WEIGHT_KEY);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? normalizeWeight(value, range.min, range) : null;
  } catch {
    return null;
  }
}

export function useLongShortHeatPreferences(
  defaultFundWeight: Ref<number>,
  weightRange: Ref<LongShortHeatWeightRange>,
) {
  const stored = storedWeight(weightRange.value);
  const hasStoredPreference = ref(stored !== null);
  const fundWeight = ref(stored ?? normalizeWeight(defaultFundWeight.value, weightRange.value.min, weightRange.value));
  const breadthWeight = computed(() => Math.round((1 - fundWeight.value) * 100) / 100);

  watch([defaultFundWeight, weightRange], ([value, range]) => {
    if (!hasStoredPreference.value) fundWeight.value = normalizeWeight(value, range.min, range);
  }, { deep: true });

  function setFundWeight(value: number): void {
    fundWeight.value = normalizeWeight(value, defaultFundWeight.value, weightRange.value);
  }

  function persist(): void {
    try {
      localStorage.setItem(LONG_SHORT_HEAT_WEIGHT_KEY, String(fundWeight.value));
      hasStoredPreference.value = true;
    } catch {
      // Browsers may disable local storage; the current-session value remains usable.
    }
  }

  function reset(): void {
    hasStoredPreference.value = false;
    fundWeight.value = normalizeWeight(defaultFundWeight.value, weightRange.value.min, weightRange.value);
    persist();
  }

  return { breadthWeight, fundWeight, setFundWeight, persist, reset };
}
