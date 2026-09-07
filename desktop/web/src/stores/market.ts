import { defineStore } from "pinia";
import { ref } from "vue";
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
    select,
  };
});
