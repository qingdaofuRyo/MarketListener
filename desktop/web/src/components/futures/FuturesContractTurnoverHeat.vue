<script setup lang="ts">
import { computed } from "vue";
import FuturesLongShortHeat from "./FuturesLongShortHeat.vue";
import type { ContractTurnoverHeatPoint, ContractTurnoverHeatResponse, LongShortHeatResponse } from "../../domain/futures";

const props = defineProps<{ payload: ContractTurnoverHeatResponse }>();

function adaptPoint(point: ContractTurnoverHeatPoint) {
  return {
    tradeDate: point.tradeDate,
    upVarietyCount: point.upContractCount,
    downVarietyCount: point.downContractCount,
    flatVarietyCount: point.flatContractCount,
    upFund: point.turnoverValidContractCount > 0 ? point.upTurnoverCny : null,
    downFund: point.turnoverValidContractCount > 0 ? point.downTurnoverCny : null,
    flatFund: point.turnoverValidContractCount > 0 ? point.flatTurnoverCny : null,
    breadthScore10: point.countScore10,
    fundScore10: point.turnoverScore10,
    divergence: point.divergence,
    isWarmup: point.isWarmup,
    dataQualityStatus: point.dataQualityStatus,
    coverage: { variety: point.coverage.direction, fund: point.coverage.turnover },
    totalDepositCny: point.totalDepositCny,
    depositCoverage: point.coverage.deposit,
  };
}

const adaptedPayload = computed<LongShortHeatResponse>(() => {
  const points = (props.payload.points || []).map(adaptPoint);
  const latest = props.payload.latest ? adaptPoint(props.payload.latest) : null;
  return {
    available: props.payload.available,
    config: {
      defaultUserWeight: {
        breadthWeight: props.payload.config.defaultUserWeight.contractWeight,
        fundWeight: props.payload.config.defaultUserWeight.turnoverWeight,
      },
      userWeight: props.payload.config.userWeight,
      score: props.payload.config.score,
      stateBands: props.payload.config.stateBands,
      divergenceThreshold: props.payload.config.divergenceThreshold,
      fundUnit: "元",
      lookbackTradingDays: props.payload.config.lookbackTradingDays,
      halfLife: props.payload.config.halfLife,
    },
    points,
    latest,
    generatedAt: props.payload.generatedAt,
    source: props.payload.source,
    formulaVersion: props.payload.formulaVersion,
    limitations: props.payload.limitations,
  };
});
</script>

<template>
  <FuturesLongShortHeat :payload="adaptedPayload" metric-mode="contract-turnover" />
</template>
