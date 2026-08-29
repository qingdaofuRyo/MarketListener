<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  fundWeight: number;
  defaultFundWeight: number;
  min: number;
  max: number;
  step: number;
  disabled?: boolean;
}>();

const emit = defineEmits<{
  "update:fundWeight": [value: number];
  commit: [];
  reset: [];
}>();

const sliderValue = computed({
  get: () => Math.round(props.fundWeight * 100),
  set: (value: number) => emit("update:fundWeight", value / 100),
});
const sliderMin = computed(() => Math.round(props.min * 100));
const sliderMax = computed(() => Math.round(props.max * 100));
const sliderStep = computed(() => Math.round(props.step * 100));
const breadthPercent = computed(() => 100 - sliderValue.value);
const defaultLabel = computed(() => `${Math.round((1 - props.defaultFundWeight) * 100)} : ${Math.round(props.defaultFundWeight * 100)}`);

function commit(): void {
  emit("commit");
}
</script>

<template>
  <section class="weight-control" aria-label="总多空热度权重">
    <div class="weight-labels">
      <span>品种热度 <strong data-test="breadth-weight">{{ breadthPercent }}%</strong></span>
      <span>资金热度 <strong data-test="fund-weight">{{ sliderValue }}%</strong></span>
    </div>
    <el-slider
      v-model="sliderValue"
      :min="sliderMin"
      :max="sliderMax"
      :step="sliderStep"
      :disabled="disabled"
      :format-tooltip="(value: number) => `资金 ${value}% · 品种 ${100 - value}%`"
      aria-label="资金热度权重"
      data-test="heat-weight-slider"
      @change="commit"
    />
    <div class="weight-footer">
      <span>单点调节，两个权重之和始终为 100%</span>
      <el-button size="small" plain data-test="heat-weight-reset" @click="emit('reset')">
        恢复默认 {{ defaultLabel }}
      </el-button>
    </div>
  </section>
</template>

<style scoped>
.weight-control {
  max-width: 760px;
  margin: 2px auto 0;
  padding: 12px 16px;
  border: 1px solid var(--ml-divider);
  border-radius: 8px;
  background: var(--ml-surface-elevated);
}
.weight-labels, .weight-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}
.weight-labels { color: var(--ml-text-secondary); font-size: 12px; }
.weight-labels strong { color: var(--ml-text-primary); font: 700 13px/1.2 ui-monospace, Consolas, monospace; }
.weight-footer { color: var(--ml-text-disabled); font-size: 11px; }
:deep(.el-slider) { margin: 8px 2px 5px; }
@media (max-width: 560px) {
  .weight-footer { align-items: flex-start; flex-direction: column; }
}
</style>
