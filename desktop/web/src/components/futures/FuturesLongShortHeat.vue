<script setup lang="ts">
import { computed, ref } from "vue";
import { useLongShortHeatPreferences } from "../../composables/useLongShortHeatPreferences";
import {
  combineHeatScores,
  finiteScore,
  heatStateLabel,
  type HeatTimeRange,
  type LongShortHeatPoint,
  type LongShortHeatResponse,
} from "../../domain/futures";
import ChartRangeSelector from "./ChartRangeSelector.vue";
import HeatWeightControl from "./HeatWeightControl.vue";
import LongShortHeatGauge from "./LongShortHeatGauge.vue";
import LongShortHeatHistoryChart from "./LongShortHeatHistoryChart.vue";

const props = defineProps<{ payload: LongShortHeatResponse }>();

const range = ref<HeatTimeRange>("1y");
const defaultFundWeight = computed(() => {
  const value = props.payload.config.defaultUserWeight.fundWeight;
  return Math.max(0, Math.min(1, value > 1 ? value / 100 : value));
});
const userWeight = computed(() => props.payload.config.userWeight);
const { breadthWeight, fundWeight, setFundWeight, persist, reset } = useLongShortHeatPreferences(
  defaultFundWeight,
  userWeight,
);

const scoreMin = computed(() => props.payload.config.score.min);
const scoreMax = computed(() => props.payload.config.score.max);

const allPoints = computed<LongShortHeatPoint[]>(() => {
  const byDate = new Map<string, LongShortHeatPoint>();
  for (const point of props.payload.points || []) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(point.tradeDate)) byDate.set(point.tradeDate, point);
  }
  const latest = props.payload.latest;
  if (latest && /^\d{4}-\d{2}-\d{2}$/.test(latest.tradeDate)) byDate.set(latest.tradeDate, latest);
  return [...byDate.values()].sort((left, right) => left.tradeDate.localeCompare(right.tradeDate));
});

function subtractRange(latest: string, selected: HeatTimeRange): string {
  if (selected === "all") return "";
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(latest);
  if (!match) return "";
  const sourceYear = Number(match[1]);
  const sourceMonth = Number(match[2]) - 1;
  const sourceDay = Number(match[3]);
  const months = ({ "1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60 } as const)[selected];
  const targetMonthIndex = sourceYear * 12 + sourceMonth - months;
  const targetYear = Math.floor(targetMonthIndex / 12);
  const targetMonth = ((targetMonthIndex % 12) + 12) % 12;
  const lastDay = new Date(Date.UTC(targetYear, targetMonth + 1, 0)).getUTCDate();
  const date = new Date(Date.UTC(targetYear, targetMonth, Math.min(sourceDay, lastDay)));
  return date.toISOString().slice(0, 10);
}

const visiblePoints = computed(() => {
  const latestDate = allPoints.value.at(-1)?.tradeDate;
  if (!latestDate) return [];
  const cutoff = subtractRange(latestDate, range.value);
  return cutoff ? allPoints.value.filter((point) => point.tradeDate >= cutoff) : allPoints.value;
});
const currentPoint = computed(() => allPoints.value.at(-1));
const currentBreadth = computed(() => finiteScore(currentPoint.value?.breadthScore10));
const currentFund = computed(() => finiteScore(currentPoint.value?.fundScore10));
const currentTotal = computed(() => combineHeatScores(
  currentBreadth.value,
  currentFund.value,
  breadthWeight.value,
  fundWeight.value,
));
const stateBands = computed(() => props.payload.config.stateBands);
const totalState = computed(() => heatStateLabel(currentTotal.value, stateBands.value));
const breadthState = computed(() => heatStateLabel(currentBreadth.value, stateBands.value));
const fundState = computed(() => heatStateLabel(currentFund.value, stateBands.value));

const divergenceText = computed(() => {
  const value = currentPoint.value?.divergence;
  if (typeof value !== "number" || !Number.isFinite(value)) return "背离状态暂无数据";
  const threshold = props.payload.config.divergenceThreshold;
  if (value >= threshold) return "品种强于资金";
  if (value <= -threshold) return "资金强于品种";
  return "品种与资金基本一致";
});

const coverageText = computed(() => {
  const coverage = currentPoint.value?.coverage;
  if (typeof coverage === "number" && Number.isFinite(coverage)) {
    const percent = coverage <= 1 ? coverage * 100 : coverage;
    return `数据覆盖率 ${percent.toFixed(1)}%`;
  }
  if (coverage && typeof coverage === "object") {
    const variety = typeof coverage.variety === "number" ? coverage.variety : undefined;
    const fund = typeof coverage.fund === "number" ? coverage.fund : undefined;
    const values = [
      variety === undefined ? "" : `品种 ${(variety <= 1 ? variety * 100 : variety).toFixed(1)}%`,
      fund === undefined ? "" : `资金 ${(fund <= 1 ? fund * 100 : fund).toFixed(1)}%`,
    ].filter(Boolean);
    if (values.length) return `数据覆盖率 ${values.join(" · ")}`;
  }
  return "数据覆盖率暂无数据";
});

function qualityTagType(): "success" | "warning" | "danger" | "info" {
  const status = currentPoint.value?.dataQualityStatus?.toUpperCase();
  if (status === "PASS" || status === "OK") return "success";
  if (status === "FAIL" || status === "INVALID") return "danger";
  if (status) return "warning";
  return "info";
}
</script>

<template>
  <section class="panel long-short-heat" data-test="futures-long-short-heat">
    <header class="heat-header">
      <div>
        <h2>多空热度</h2>
        <p>品种数量广度与沉淀资金方向的最近 10 个交易日指数衰减结果</p>
      </div>
      <div class="heat-meta">
        <el-tag v-if="currentPoint" size="small" effect="plain">{{ currentPoint.tradeDate }}</el-tag>
        <el-tag v-if="currentPoint?.isWarmup" size="small" type="warning" effect="plain">预热期</el-tag>
        <el-tag size="small" :type="qualityTagType()" effect="plain">{{ currentPoint?.dataQualityStatus || "质量未知" }}</el-tag>
      </div>
    </header>

    <div class="gauge-grid" data-test="heat-gauge-grid">
      <LongShortHeatGauge
        title="总多空热度"
        :value="currentTotal"
        :status="totalState"
        :bands="stateBands"
        :min="scoreMin"
        :max="scoreMax"
        data-test-kind="total"
      />
      <LongShortHeatGauge
        title="品种多空热度"
        :value="currentBreadth"
        :status="breadthState"
        :bands="stateBands"
        :min="scoreMin"
        :max="scoreMax"
        data-test-kind="breadth"
      />
      <LongShortHeatGauge
        title="资金多空热度"
        :value="currentFund"
        :status="fundState"
        :bands="stateBands"
        :min="scoreMin"
        :max="scoreMax"
        data-test-kind="fund"
      />
    </div>

    <HeatWeightControl
      :fund-weight="fundWeight"
      :default-fund-weight="defaultFundWeight"
      :min="userWeight.min"
      :max="userWeight.max"
      :step="userWeight.step"
      @update:fund-weight="setFundWeight"
      @commit="persist"
      @reset="reset"
    />

    <div class="heat-summary" aria-live="polite">
      <span>{{ divergenceText }}</span>
      <span>{{ coverageText }}</span>
      <span v-if="currentPoint">有效样本：{{ currentPoint.upVarietyCount + currentPoint.downVarietyCount }} · 平盘：{{ currentPoint.flatVarietyCount }}</span>
    </div>

    <section class="history-section">
      <header class="history-header">
        <div><h3>多空热度历史趋势</h3><p>各点均为截至该交易日的 10 日加权结果，0 轴表示多空均衡。</p></div>
        <ChartRangeSelector v-model="range" />
      </header>
      <LongShortHeatHistoryChart
        :points="visiblePoints"
        :breadth-weight="breadthWeight"
        :fund-weight="fundWeight"
        :fund-unit="payload.config.fundUnit"
        :min="scoreMin"
        :max="scoreMax"
      />
    </section>
  </section>
</template>

<style scoped>
.long-short-heat { min-width: 0; }
.heat-header, .history-header, .heat-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}
.heat-header h2, .history-header h3 { margin: 0; }
.heat-header p, .history-header p { margin: 5px 0 0; color: var(--ml-text-secondary); font-size: 12px; }
.heat-meta { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 6px; }
.gauge-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 15px 0 12px;
}
.heat-summary {
  justify-content: center;
  flex-wrap: wrap;
  margin: 12px 0 0;
  color: var(--ml-text-secondary);
  font-size: 12px;
}
.heat-summary span + span::before { content: "·"; margin-right: 14px; color: var(--ml-text-disabled); }
.history-section { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--ml-divider); }
.history-header { align-items: flex-start; margin-bottom: 4px; }
@media (max-width: 1050px) {
  .gauge-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 700px) {
  .heat-header, .history-header { align-items: stretch; flex-direction: column; }
  .heat-meta { justify-content: flex-start; }
  .gauge-grid { grid-template-columns: 1fr; }
  .heat-summary { align-items: flex-start; flex-direction: column; gap: 5px; }
  .heat-summary span + span::before { content: none; }
}
</style>
