<script setup lang="ts">
import type { HeatTimeRange } from "../../domain/futures";

defineProps<{ modelValue: HeatTimeRange }>();
const emit = defineEmits<{ "update:modelValue": [value: HeatTimeRange] }>();

const ranges: Array<[HeatTimeRange, string]> = [
  ["1m", "1个月"],
  ["3m", "3个月"],
  ["6m", "6个月"],
  ["1y", "1年"],
  ["3y", "3年"],
  ["5y", "5年"],
  ["all", "全部"],
];
</script>

<template>
  <el-radio-group
    :model-value="modelValue"
    size="small"
    aria-label="多空热度历史时间范围"
    data-test="heat-range-selector"
    @update:model-value="emit('update:modelValue', $event as HeatTimeRange)"
  >
    <el-radio-button v-for="[value, label] in ranges" :key="value" :value="value">{{ label }}</el-radio-button>
  </el-radio-group>
</template>

<style scoped>
:deep(.el-radio-group) { display: flex; flex-wrap: wrap; }
</style>
