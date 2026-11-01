<script setup lang="ts">
import { computed } from "vue";
import zhCn from "element-plus/es/locale/lang/zh-cn";

const props = defineProps<{ dates: string[]; modelValue: string; label: string }>();
const emit = defineEmits<{ (event: "update:modelValue", day: string): void; (event: "open"): void }>();
const tradingDays = computed(() => new Set(props.dates));
function localDay(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function disabledDate(date: Date): boolean { return !tradingDays.value.has(localDay(date)); }
function selectDate(day: string | null): void {
  if (day && tradingDays.value.has(day)) emit("update:modelValue", day);
}
function calendarVisibility(visible: boolean): void { if (visible) emit("open"); }
</script>

<template>
  <div class="trading-date-picker" data-test="futures-trading-date-picker" :data-calendar-label="label" @pointerdown="emit('open')">
    <el-config-provider :locale="zhCn">
    <el-date-picker :model-value="modelValue || undefined" type="date" value-format="YYYY-MM-DD" format="YYYY-MM-DD"
      :aria-label="label" :disabled="!dates.length" :disabled-date="disabledDate" :clearable="false"
      :editable="false"
      placeholder="选择交易日" popper-class="futures-trading-calendar" @update:model-value="selectDate"
      @visible-change="calendarVisibility">
      <template #default="cell">
        <div class="trading-date-cell" :class="{ selected: cell.isCurrent }"
          :data-day="cell.dayjs.format('YYYY-MM-DD')" :data-trading-day="tradingDays.has(cell.dayjs.format('YYYY-MM-DD'))">
          <span>{{ cell.text }}</span><i v-if="tradingDays.has(cell.dayjs.format('YYYY-MM-DD'))" class="trading-day-dot" aria-hidden="true" />
        </div>
      </template>
    </el-date-picker>
    </el-config-provider>
  </div>
</template>

<style scoped>
.trading-date-picker { flex-shrink: 0; }
.trading-date-picker :deep(.el-date-editor) { width: 152px; }
.trading-date-picker :deep(.el-input__wrapper) { min-height: 34px; border-radius: 7px; background: var(--ml-surface); transition: box-shadow 150ms; }
.trading-date-picker :deep(.el-input__wrapper:hover) { box-shadow: 0 0 0 1px color-mix(in srgb,var(--ml-accent) 40%,var(--ml-divider)) inset; }
.trading-date-picker :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 2px var(--ml-accent) inset; }
.trading-date-picker :deep(.el-input__inner) { font: 12px ui-monospace,Consolas,monospace; font-variant-numeric: tabular-nums; }
.trading-date-cell { position: relative; display: flex; align-items: center; justify-content: center; width: 30px; height: 30px; margin: auto; border-radius: 9px; }
.trading-date-cell.selected { color: #fff; background: var(--ml-accent); }
.trading-day-dot { position: absolute; right: 2px; top: 2px; width: 5px; height: 5px; border-radius: 50%; background: #e9b62e; box-shadow: 0 0 0 1px var(--ml-surface); }
@media (prefers-reduced-motion: reduce) { .trading-date-picker :deep(.el-input__wrapper) { transition: none; } }
</style>
