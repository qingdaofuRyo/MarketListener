import { defineStore } from "pinia";
import { ref } from "vue";
import { resolveSecondaryMetric, type MeasureBar, type MeasureEvidence } from "../domain/chartSubchart";
import {
  DEFAULT_MARKET_CATEGORY,
  type MarketSortState,
  type SortableMarketInstrument,
} from "../domain/marketList";

/** Route-independent context shared by the all-market and detail pages. */
export const useMarketStore = defineStore("market", () => {
  const category = ref(DEFAULT_MARKET_CATEGORY);
  const query = ref("");
  const sort = ref<MarketSortState>({ field: null, direction: null });
  const selectedId = ref("");
  const items = ref<SortableMarketInstrument[]>([]);
  const boardTopPeriod = ref("1d");
  const boardBottomPeriod = ref("1h");
  const detailPeriod = ref("1d");
  const interestInstruments = ref<string[]>([]);

  function observeMeasures(id: string, evidence: MeasureEvidence | undefined, bars: readonly MeasureBar[]): void {
    if (!interestInstruments.value.includes(id) && resolveSecondaryMetric(evidence, bars) === "openInterest") interestInstruments.value.push(id);
  }

  function selectDetailPeriod(period: string, available: readonly string[]): boolean {
    if (!available.includes(period)) return false;
    detailPeriod.value = period;
    return true;
  }

  function select(instrumentId: string): void {
    selectedId.value = instrumentId;
  }

  return {
    category,
    query,
    sort,
    selectedId,
    items,
    boardTopPeriod,
    boardBottomPeriod,
    detailPeriod,
    interestInstruments,
    observeMeasures,
    selectDetailPeriod,
    select,
  };
});
